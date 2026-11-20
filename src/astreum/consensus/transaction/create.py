from __future__ import annotations

from typing import Optional, Union

from astreum.expression import Expr, NIL, bytes_, int_, link
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.treasury.record import LoanType

RECIPIENT_SIZE = 32
SIGNATURE_SIZE = 64
LOAN_TRANSACTION_ID_SIZE = 32
PROGRAM_HASH_SIZE = 32
EXPR_LIST_ID_SIZE = 32


def create_transaction(
    *,
    chain_id: int,
    sender: bytes,
    counter: int,
    recipient: bytes,
    code: TransactionCode = TransactionCode.TRANSFER,
    amount: int = 0,
    cost_limit: int = 0,
    base_fee: int = 1,
    secret_key=None,
    loan_transaction_id: Optional[bytes] = None,
    loan_type: Union[int, LoanType] = LoanType.SECURED,
    counterparty: Optional[bytes] = None,
    new_withdrawal_window: Optional[int] = None,
    withdraw_signature: Optional[bytes] = None,
    withdraw_counter: Optional[int] = None,
    withdraw_amount: Optional[int] = None,
    payer: Optional[bytes] = None,
    channel_close_op: bool = False,
    expr_list_id: Optional[bytes] = None,
    program_hash: Optional[bytes] = None,
    data: Expr = NIL,
) -> Transaction:
    _validate_params(
        code=code,
        amount=amount,
        recipient=recipient,
        loan_transaction_id=loan_transaction_id,
        loan_type=loan_type,
        counterparty=counterparty,
        withdraw_signature=withdraw_signature,
        withdraw_counter=withdraw_counter,
        withdraw_amount=withdraw_amount,
        payer=payer,
        expr_list_id=expr_list_id,
        program_hash=program_hash,
    )

    data = _build_data_expr(
        code=code,
        data=data,
        loan_transaction_id=loan_transaction_id,
        loan_type=loan_type,
        counterparty=counterparty,
        new_withdrawal_window=new_withdrawal_window,
        withdraw_signature=withdraw_signature,
        withdraw_counter=withdraw_counter,
        withdraw_amount=withdraw_amount,
        channel_close_op=channel_close_op,
        expr_list_id=expr_list_id,
        program_hash=program_hash,
    )

    tx = Transaction(
        chain_id=chain_id,
        amount=amount,
        code=code,
        counter=counter,
        cost_limit=cost_limit,
        base_fee=base_fee,
        data=data,
        recipient=recipient,
        sender=sender,
    )

    if secret_key is not None:
        tx.sign(secret_key)

    return tx


def _validate_params(
    *,
    code: TransactionCode,
    amount: int,
    recipient: Optional[bytes] = None,
    loan_transaction_id: Optional[bytes] = None,
    loan_type: Union[int, LoanType] = LoanType.SECURED,
    counterparty: Optional[bytes] = None,
    withdraw_signature: Optional[bytes] = None,
    withdraw_counter: Optional[int] = None,
    withdraw_amount: Optional[int] = None,
    payer: Optional[bytes] = None,
    expr_list_id: Optional[bytes] = None,
    program_hash: Optional[bytes] = None,
) -> None:
    match code:
        case TransactionCode.TRANSFER:
            if recipient is None:
                raise ValueError("TRANSFER requires recipient")

        case TransactionCode.CHANNEL_UPDATE:
            if recipient is None or len(recipient) != RECIPIENT_SIZE:
                raise ValueError("CHANNEL_UPDATE requires recipient (32 bytes)")
            if counterparty is None or len(counterparty) != RECIPIENT_SIZE:
                raise ValueError("CHANNEL_UPDATE requires counterparty (32 bytes)")
            if amount < 0:
                raise ValueError("CHANNEL_UPDATE amount must be >= 0")

        case TransactionCode.CHANNEL_WITHDRAW:
            if recipient is None or len(recipient) != RECIPIENT_SIZE:
                raise ValueError("CHANNEL_WITHDRAW requires recipient (32 bytes)")
            if counterparty is None or len(counterparty) != RECIPIENT_SIZE:
                raise ValueError("CHANNEL_WITHDRAW requires counterparty (32 bytes)")
            if withdraw_signature is None or len(withdraw_signature) != SIGNATURE_SIZE:
                raise ValueError("CHANNEL_WITHDRAW requires withdraw_signature (64 bytes)")
            if withdraw_counter is None or withdraw_counter < 0:
                raise ValueError("CHANNEL_WITHDRAW requires withdraw_counter >= 0")
            if withdraw_amount is None or withdraw_amount < 0:
                raise ValueError("CHANNEL_WITHDRAW requires withdraw_amount >= 0")
            if payer is None or len(payer) != RECIPIENT_SIZE:
                raise ValueError("CHANNEL_WITHDRAW requires payer (32 bytes)")

        case TransactionCode.CHANNEL_CLOSE:
            if recipient is None or len(recipient) != RECIPIENT_SIZE:
                raise ValueError("CHANNEL_CLOSE requires recipient (32 bytes)")
            if counterparty is None or len(counterparty) != RECIPIENT_SIZE:
                raise ValueError("CHANNEL_CLOSE requires counterparty (32 bytes)")

        case TransactionCode.TREASURY_DEPOSIT:
            if amount <= 0:
                raise ValueError("TREASURY_DEPOSIT requires amount > 0")

        case TransactionCode.TREASURY_BORROW:
            if amount <= 0:
                raise ValueError("TREASURY_BORROW requires amount > 0")

        case TransactionCode.TREASURY_REPAY:
            if amount <= 0:
                raise ValueError("TREASURY_REPAY requires amount > 0")
            if loan_transaction_id is None or len(loan_transaction_id) != LOAN_TRANSACTION_ID_SIZE:
                raise ValueError("TREASURY_REPAY requires loan_transaction_id (32 bytes)")

        case TransactionCode.TREASURY_CLOSE:
            if amount <= 0:
                raise ValueError("TREASURY_CLOSE requires amount > 0")
            if loan_transaction_id is None or len(loan_transaction_id) != LOAN_TRANSACTION_ID_SIZE:
                raise ValueError("TREASURY_CLOSE requires loan_transaction_id (32 bytes)")

        case TransactionCode.STORAGE_CREATE:
            if expr_list_id is None or len(expr_list_id) != EXPR_LIST_ID_SIZE:
                raise ValueError("STORAGE_CREATE requires expr_list_id (32 bytes)")

        case TransactionCode.CODE_ACCOUNT_CREATE:
            if program_hash is None or len(program_hash) != PROGRAM_HASH_SIZE:
                raise ValueError("CODE_ACCOUNT_CREATE requires program_hash (32 bytes)")

        case TransactionCode.CODE_ACCOUNT_CALL:
            if recipient is None:
                raise ValueError("CODE_ACCOUNT_CALL requires recipient")


def _build_data_expr(
    *,
    code: TransactionCode,
    data: Expr,
    loan_transaction_id: Optional[bytes] = None,
    loan_type: Union[int, LoanType] = LoanType.SECURED,
    counterparty: Optional[bytes] = None,
    new_withdrawal_window: Optional[int] = None,
    withdraw_signature: Optional[bytes] = None,
    withdraw_counter: Optional[int] = None,
    withdraw_amount: Optional[int] = None,
    channel_close_op: bool = False,
    expr_list_id: Optional[bytes] = None,
    program_hash: Optional[bytes] = None,
) -> Expr:
    match code:
        case TransactionCode.CHANNEL_UPDATE:
            result = link(bytes_(counterparty), NIL)  # type: ignore[arg-type]
            if new_withdrawal_window is not None:
                result = link(int_(new_withdrawal_window), result)
            return result

        case TransactionCode.CHANNEL_WITHDRAW:
            return link(
                int_(withdraw_counter),  # type: ignore[arg-type]
                link(
                    int_(withdraw_amount),  # type: ignore[arg-type]
                    link(bytes_(withdraw_signature), NIL),  # type: ignore[arg-type]
                ),
            )

        case TransactionCode.CHANNEL_CLOSE:
            return link(bytes_(counterparty), NIL)  # type: ignore[arg-type]

        case TransactionCode.TREASURY_BORROW:
            return link(int_(LoanType(loan_type)), NIL)

        case TransactionCode.TREASURY_REPAY | TransactionCode.TREASURY_CLOSE:
            return link(bytes_(loan_transaction_id), NIL)

        case TransactionCode.STORAGE_CREATE:
            return link(Expr("link", head_hash=expr_list_id), NIL)

        case TransactionCode.CODE_ACCOUNT_CREATE:
            return link(Expr("link", head_hash=program_hash), NIL)

        case TransactionCode.STORAGE_PAYMENT | TransactionCode.CODE_ACCOUNT_CALL:
            return data

    return data
