from __future__ import annotations

from dataclasses import replace
from typing import Any, Tuple

from astreum.expression import Expr, NIL
from astreum.expression import ZERO32
from astreum.expression.helpers import exprs_to_linked_expr
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS
from astreum.consensus.account import create_account
from astreum.consensus.account.model import generate_new_account_storage_contracts
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS, Receipt
from astreum.consensus.block.rate import calculate_storage_fee
from astreum.crypto.bloom_search import make_search_variants
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.from_storage import get_transaction_from_storage
from astreum.consensus.transaction.accounts.create import handle_expression_account_create
from astreum.consensus.transaction.accounts.expression import handle_expression_account_call
from astreum.consensus.transaction.channel.close import handle_channel_close
from astreum.consensus.transaction.channel.update import handle_channel_update
from astreum.consensus.transaction.channel.withdraw import handle_channel_withdraw
from astreum.consensus.transaction.storage.initial import handle_storage_initial_contract
from astreum.consensus.transaction.storage.pending import add_pending_storage_contract
from astreum.consensus.transaction.storage.payment import handle_storage_payment_contract
from astreum.consensus.transaction.treasury.borrow import handle_treasury_borrow
from astreum.consensus.transaction.bloom.pending import finalize_pending_bloom_inserts
from astreum.consensus.transaction.treasury.record import (
    TreasuryUserRecord,
)
from astreum.consensus.transaction.treasury.close import handle_treasury_close
from astreum.consensus.transaction.treasury.repay import handle_treasury_repay
from astreum.consensus.transaction.treasury.offers import handle_treasury_sell


# Transaction codes whose handler credits the sender mid-flow (so a pre-check on
# the sender's *initial* balance is invalid). These may self-fund via the credit.
_CREDIT_FUNDING_CODES = {TransactionCode.STORAGE_PAYMENT}


def _data_nodes(data: Expr) -> list[Expr]:
    result = []
    current = data
    while current is not None and current._tag == "link":
        if current._head is not None:
            result.append(current._head)
        current = current._tail
    return result


def _apply_tx_effects(
    node: Any,
    block: object,
    transaction: Any,
    transaction_hash: bytes,
    *,
    nested: bool = False,
) -> Tuple[int, int, int, list]:
    """Apply a transaction's effects.

    When *nested* is False (top-level apply_transaction): deducts all fees from
    the sender and records consensus artifacts (bloom, receipt, trie).

    When *nested* is True (tx.new): deducts only the transfer amount from the
    sender (the contract). Execution and storage fees are returned for the
    caller to charge to the parent meter. No consensus recording.

    Returns (receipt_status, execution_fee, storage_fee, collected_logs).
    """
    if transaction.chain_id != block.chain_id:
        raise ValueError("transaction chain_id does not match block chain_id")

    sender_account = block.accounts.get_account(address=transaction.sender, node=node)
    if sender_account is None:
        raise ValueError("sender account not found")
    storage_account = block.accounts.get_account(address=STORAGE_ADDRESS, node=node)

    receipt_status = STATUS_SUCCESS
    tx_fee = 1 if not nested else 0

    # Pre-fee guard: top-level sender must always cover at least the base tx_fee.
    # Credit-funding codes are exempt — their handler credits the sender before
    # the final affordability check.
    if (
        not nested
        and sender_account.balance < tx_fee
        and transaction.code not in _CREDIT_FUNDING_CODES
    ):
        raise ValueError("insufficient balance for transaction fee")

    transfer_amount = transaction.amount
    if transaction.code in (
        TransactionCode.CHANNEL_WITHDRAW,
        TransactionCode.TREASURY_BORROW,
        TransactionCode.TREASURY_SELL,
    ):
        transfer_amount = 0

    # Counter guard (top-level only): a valid tx carries counter equal to the
    # sender account's counter; processing increments it. A mismatch fails the
    # receipt (fees still charged) without consuming the sender's counter, so
    # junk txs cannot grief-bump an account's counter. Nested txs are stamped
    # and incremented by the tx.new operator itself.
    counter_matched = nested or (transaction.counter == sender_account.counter)
    if not counter_matched:
        receipt_status = STATUS_FAILED
        transfer_amount = 0

    if (
        transaction.recipient == STORAGE_ADDRESS
        and transaction.code == TransactionCode.STORAGE_CREATE
        and transfer_amount > 0
    ):
        receipt_status = STATUS_FAILED
        transfer_amount = 0
    max_execution_fee = (
        max(0, transaction.cost_limit)
        if transaction.code == TransactionCode.CODE_ACCOUNT_CALL
        else 0
    )

    # Pre-match affordability guard.
    if nested:
        # Nested: contract only needs to cover the value it sends.
        if sender_account.balance < transfer_amount:
            receipt_status = STATUS_FAILED
            transfer_amount = 0
    else:
        # Top-level: historical combined check. Credit-funding codes are exempt
        # — their handler credits the sender before the final affordability check.
        if (
            transaction.code not in _CREDIT_FUNDING_CODES
            and sender_account.balance < tx_fee + transfer_amount + max_execution_fee
        ):
            receipt_status = STATUS_FAILED
            transfer_amount = 0

    def _get_or_create_recipient_account() -> tuple["Account", bool]:
        if transaction.recipient == transaction.sender:
            return sender_account, False
        if transaction.recipient == STORAGE_ADDRESS:
            return storage_account, False
        account = block.accounts.get_account(address=transaction.recipient, node=node)
        if account is None:
            return (create_account(), True)

        return (account, False)

    recipient_account = None
    is_recipient_new = False

    # data fee is sender + receiptient sizes
    current_data_fee = 0
    current_evaluation_fee = 0
    current_storage_fee = 0
    collected_logs: list[Expr] = []
    receipt_mint = 0

    match transaction.code:
        case TransactionCode.TRANSFER:
            (recipient_account, is_recipient_new) = _get_or_create_recipient_account()
            recipient_account.balance += transfer_amount

        case TransactionCode.CHANNEL_UPDATE:
            if transaction.recipient != transaction.sender:
                receipt_status = STATUS_FAILED
                transfer_amount = 0
            else:
                recipient_account = sender_account
                if receipt_status == STATUS_SUCCESS:
                    nodes = _data_nodes(transaction.data)
                    if len(nodes) < 1 or len(nodes) > 2:
                        receipt_status = STATUS_FAILED
                        transfer_amount = 0
                    else:
                        cp_node = nodes[-1]
                        if cp_node._tag != "bytes":
                            receipt_status = STATUS_FAILED
                            transfer_amount = 0
                        else:
                            counterparty = cp_node.value
                            window = (
                                nodes[0].value
                                if len(nodes) == 2 and nodes[0]._tag == "int"
                                else None
                            )
                            channel_update_success = handle_channel_update(
                                node=node,
                                block=block,
                                sender_account=sender_account,
                                counterparty=counterparty,
                                new_withdrawal_window=window,
                                tx_amount=transaction.amount,
                            )
                    if not channel_update_success:
                        receipt_status = STATUS_FAILED
                        transfer_amount = 0

        case TransactionCode.CHANNEL_WITHDRAW:
            transfer_amount = 0
            recipient_account = block.accounts.get_account(address=transaction.recipient, node=node)
            if receipt_status == STATUS_SUCCESS:
                if recipient_account is None:
                    receipt_status = STATUS_FAILED
                else:
                    channel_withdraw_success = handle_channel_withdraw(
                        node=node,
                        block=block,
                        sender_account=sender_account,
                        transaction=transaction,
                    )
                    if not channel_withdraw_success:
                        receipt_status = STATUS_FAILED

        case TransactionCode.CHANNEL_CLOSE:
            transfer_amount = 0
            if transaction.recipient != transaction.sender:
                receipt_status = STATUS_FAILED
            elif receipt_status == STATUS_SUCCESS:
                nodes = _data_nodes(transaction.data)
                if len(nodes) != 1 or nodes[0]._tag != "bytes":
                    receipt_status = STATUS_FAILED
                else:
                    channel_close_success = handle_channel_close(
                        node=node,
                        block=block,
                        sender_account=sender_account,
                        counterparty=nodes[0].value,
                    )
                    if not channel_close_success:
                        receipt_status = STATUS_FAILED

        case TransactionCode.TREASURY_DEPOSIT:
            (recipient_account, is_recipient_new) = _get_or_create_recipient_account()
            if transaction.recipient != TREASURY_ADDRESS:
                transfer_amount = 0
                pass
            elif receipt_status == STATUS_SUCCESS:
                stake_trie = recipient_account.data
                existing_record_head = get_from_radix_tree(stake_trie, node, transaction.sender)
                if not existing_record_head or existing_record_head == ZERO32:
                    receipt_status = STATUS_FAILED
                    transfer_amount = 0
                else:
                    treasury_user_record = TreasuryUserRecord.from_storage(
                        node,
                        existing_record_head,
                    )
                    if treasury_user_record is None:
                        receipt_status = STATUS_FAILED
                        transfer_amount = 0
                    else:
                        updated_stake_record = replace(
                            treasury_user_record,
                            balance=treasury_user_record.balance + transfer_amount,
                        )
                        updated_record_head = updated_stake_record.expr().hash()
                        put_in_radix_tree(stake_trie, node, transaction.sender, updated_record_head)
                recipient_account.data_hash = stake_trie.root_hash or ZERO32
                recipient_account.balance += transfer_amount

        case TransactionCode.TREASURY_BORROW:
            transfer_amount = 0
            treasury_account = block.accounts.get_account(address=TREASURY_ADDRESS, node=node)
            recipient_account = treasury_account
            if receipt_status == STATUS_SUCCESS:
                receipt_status = handle_treasury_borrow(
                    node=node,
                    block=block,
                    transaction=transaction,
                    transaction_hash=transaction_hash,
                    sender_account=sender_account,
                    treasury_account=treasury_account,
                )

        case TransactionCode.TREASURY_CLOSE:
            treasury_account = block.accounts.get_account(address=TREASURY_ADDRESS, node=node)
            if treasury_account is not None:
                recipient_account = treasury_account
            if receipt_status == STATUS_SUCCESS:
                receipt_status = handle_treasury_close(
                    node=node,
                    block=block,
                    transaction=transaction,
                )
                if receipt_status != STATUS_SUCCESS:
                    transfer_amount = 0
            else:
                transfer_amount = 0

        case TransactionCode.TREASURY_REPAY:
            treasury_account = block.accounts.get_account(address=TREASURY_ADDRESS, node=node)
            if transaction.recipient == TREASURY_ADDRESS:
                recipient_account = treasury_account
            if receipt_status == STATUS_SUCCESS:
                receipt_status = handle_treasury_repay(
                    node=node,
                    block=block,
                    transaction=transaction,
                )
                if receipt_status != STATUS_SUCCESS:
                    transfer_amount = 0
            else:
                transfer_amount = 0

        case TransactionCode.TREASURY_SELL:
            transfer_amount = 0
            treasury_account = block.accounts.get_account(address=TREASURY_ADDRESS, node=node)
            recipient_account = treasury_account
            if receipt_status == STATUS_SUCCESS:
                receipt_status = handle_treasury_sell(
                    node=node,
                    block=block,
                    transaction=transaction,
                    transaction_hash=transaction_hash,
                    sender_account=sender_account,
                    treasury_account=treasury_account,
                )

        case TransactionCode.STORAGE_CREATE:
            (recipient_account, is_recipient_new) = _get_or_create_recipient_account()
            if transaction.recipient != STORAGE_ADDRESS:
                transfer_amount = 0
                pass
            else:
                if transfer_amount > 0:
                    receipt_status = STATUS_FAILED
                    transfer_amount = 0
                if receipt_status == STATUS_SUCCESS:
                    nodes = _data_nodes(transaction.data)
                    if len(nodes) != 1 or nodes[0]._tag != "link" or (
                        nodes[0]._head_hash is None and nodes[0]._head is None
                    ):
                        receipt_status = STATUS_FAILED
                    else:
                        expr_list_id = (
                            nodes[0]._head_hash
                            if nodes[0]._head_hash is not None
                            else nodes[0]._head.hash()
                        )
                        initial_contract_storage_fee = handle_storage_initial_contract(
                            node=node,
                            block=block,
                            transaction=transaction,
                            sender_account=sender_account,
                            storage_account=storage_account,
                            expr_list_id=expr_list_id,
                            current_fees=tx_fee + transfer_amount,
                        )
                        if initial_contract_storage_fee is None:
                            receipt_status = STATUS_FAILED
                if transfer_amount > 0:
                    recipient_account.balance += transfer_amount

        case TransactionCode.STORAGE_PAYMENT:
            (recipient_account, is_recipient_new) = _get_or_create_recipient_account()
            if transaction.recipient != STORAGE_ADDRESS:
                transfer_amount = 0
                pass
            else:
                if receipt_status == STATUS_SUCCESS:
                    payment_contract_success, total_minted = handle_storage_payment_contract(
                        node=node,
                        block=block,
                        transaction=transaction,
                        sender_account=sender_account,
                        storage_account=storage_account,
                        payload=transaction.data,
                    )
                    if not payment_contract_success:
                        receipt_status = STATUS_FAILED
                    receipt_mint = total_minted
                if transfer_amount > 0:
                    recipient_account.balance += transfer_amount

        case TransactionCode.STORAGE_REMOVE:
            (recipient_account, is_recipient_new) = _get_or_create_recipient_account()
            transfer_amount = 0
            pass

        case TransactionCode.CODE_ACCOUNT_CREATE:
            if receipt_status == STATUS_SUCCESS:
                expression_create_success = handle_expression_account_create(
                    node=node,
                    block=block,
                    transaction=transaction,
                    transaction_hash=transaction_hash,
                )
                if not expression_create_success:
                    receipt_status = STATUS_FAILED
                    transfer_amount = 0
            else:
                transfer_amount = 0

        case TransactionCode.CODE_ACCOUNT_CALL:
            if receipt_status == STATUS_SUCCESS:
                receipt_status, used_eval, nested_storage, collected_logs = handle_expression_account_call(
                    node=node,
                    block=block,
                    transaction=transaction,
                )
                current_evaluation_fee = int(used_eval)
                tx_fee += current_evaluation_fee
                if receipt_status != STATUS_SUCCESS:
                    transfer_amount = 0
                else:
                    sender_account = block.accounts.get_account(
                        address=transaction.sender,
                        node=node,
                    )
                    storage_account = block.accounts.get_account(
                        address=STORAGE_ADDRESS,
                        node=node,
                    )
                    recipient_account = block.accounts.get_account(
                        address=transaction.recipient,
                        node=node,
                    )
            else:
                transfer_amount = 0

        case _:
            (recipient_account, is_recipient_new) = _get_or_create_recipient_account()
            transfer_amount = 0
            pass

    # Bloom Filter — top-level only.
    if not nested:
        bloom_inserts: list[bytes] = make_search_variants(
            tx_hash=transaction_hash,
            sender=transaction.sender,
            receiver=transaction.recipient,
        )

        block_offset = block.height % 1024
        block.bloom_tree.insert(block_offset, bloom_inserts)

        bloom_storage_fee = calculate_storage_fee(block, 2 * len(bloom_inserts))
        current_storage_fee += bloom_storage_fee
        if sender_account.balance < (
            tx_fee + transfer_amount + current_data_fee
            + current_evaluation_fee + current_storage_fee
        ):
            receipt_status = STATUS_FAILED

        operator_bloom_fee = finalize_pending_bloom_inserts(node, block, transaction, receipt_status)
        current_storage_fee += operator_bloom_fee
        if sender_account.balance < (
            tx_fee + transfer_amount + current_data_fee
            + current_evaluation_fee + current_storage_fee
        ):
            receipt_status = STATUS_FAILED
    else:
        transaction.pending_bloom_keys.clear()
        transaction.pending_bloom_inserts.clear()

    # Transaction expr storage contract — top-level only.
    if not nested:
        transaction_storage_fee = add_pending_storage_contract(node, block, None, None, transaction.expr())
        if transaction_storage_fee is None:
            receipt_status = STATUS_FAILED
        else:
            current_storage_fee += transaction_storage_fee
            if sender_account.balance < (
                tx_fee + transfer_amount + current_data_fee
                + current_evaluation_fee + current_storage_fee
            ):
                receipt_status = STATUS_FAILED

    # Logs — both top-level and nested (nested logs bubble to outer machine).
    logs_expr = NIL
    if collected_logs:
        logs_expr = exprs_to_linked_expr(collected_logs)
        logs_storage_fee = add_pending_storage_contract(node, block, None, None, logs_expr)
        if logs_storage_fee is None:
            receipt_status = STATUS_FAILED
        else:
            current_storage_fee += logs_storage_fee
            if not nested and sender_account.balance < (
                tx_fee + transfer_amount + current_data_fee
                + current_evaluation_fee + current_storage_fee
            ):
                receipt_status = STATUS_FAILED

    # Receipt — top-level only.
    receipt = None
    if not nested:
        receipt = Receipt(
            transaction_hash=transaction_hash,
            transaction_fee=tx_fee,
            storage_fee=current_storage_fee,
            data_fee=current_data_fee,
            execution_fee=current_evaluation_fee,
            status=receipt_status,
            logs_hash=logs_expr.hash(),
            mint=receipt_mint,
        )

        receipt_storage_fee = add_pending_storage_contract(node, block, None, None, receipt.expr())
        if receipt_storage_fee is None:
            receipt_status = STATUS_FAILED

    # Final affordability check — top-level only.
    affordability_failed = False
    if not nested:
        if sender_account.balance < (
            tx_fee + transfer_amount + current_data_fee
            + current_evaluation_fee + current_storage_fee
        ):
            receipt_status = STATUS_FAILED
            # Revert any amount already credited to the recipient, then skip
            # fee deduction entirely so the sender balance never goes negative.
            if transfer_amount and recipient_account is not None:
                recipient_account.balance -= transfer_amount
            transfer_amount = 0
            affordability_failed = True

    # Balance deductions.
    if not affordability_failed:
        if nested:
            sender_account.balance -= transfer_amount
        else:
            total_fees = (
                tx_fee + current_data_fee + current_evaluation_fee + current_storage_fee
            )
            sender_account.balance -= total_fees + transfer_amount
            storage_account.balance += current_storage_fee

        # New account storage.
        if is_recipient_new:
            new_account_storage_fee = calculate_storage_fee(block, recipient_account.expr().size())
            current_storage_fee += new_account_storage_fee

            generate_new_account_storage_contracts(node, block, storage_account, recipient_account.expr())

    # Accounts write-back (cache).
    if not nested and counter_matched:
        sender_account.counter += 1
    if recipient_account is not None:
        block.accounts.set_account(transaction.recipient, recipient_account)
    block.accounts.set_account(transaction.sender, sender_account)
    if not nested:
        block.accounts.set_account(STORAGE_ADDRESS, storage_account)

    # Consensus recording — top-level only.
    if not nested:
        if block.transactions is None:
            block.transactions = []
        block.transactions.append(transaction)

        if block.receipts_trie is None:
            block.receipts_trie = RadixTree()
        put_in_radix_tree(block.receipts_trie, node, transaction_hash, receipt.expr())

        if block.receipts is None:
            block.receipts = []
        block.receipts.append(receipt)

    storage_fee = current_storage_fee if nested else 0
    return receipt_status, current_evaluation_fee, storage_fee, collected_logs


def apply_transaction(node: Any, block: object, transaction_hash: bytes) -> None:
    """Apply a transaction, collecting results on *block* (top-level entry
    point used by block verification / production)."""
    transaction = get_transaction_from_storage(node, transaction_hash)
    _apply_tx_effects(node, block, transaction, transaction_hash)


def apply_transaction_obj(node: Any, block: object, transaction: Any) -> None:
    """Apply an already-decoded Transaction (whole-message path) without
    re-parsing or fetching from storage."""
    _apply_tx_effects(node, block, transaction, transaction.hash)