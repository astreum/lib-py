from __future__ import annotations

from typing import Optional, Sequence, Tuple, Union

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
    secret_key=None,
    loan_transaction_id: Optional[bytes] = None,
    payment_interval_blocks: Optional[int] = None,
    payment_count: Optional[int] = None,
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
    limit: Optional[int] = None,
    duration: Optional[int] = None,
    price: Optional[int] = None,
    expiry: Optional[int] = None,
    offer_refs: Optional[Sequence[Tuple[bytes, bytes]]] = None,
    data: Expr = NIL,
) -> Transaction:
    _validate_params(
        code=code,
        amount=amount,
        recipient=recipient,
        loan_transaction_id=loan_transaction_id,
        payment_interval_blocks=payment_interval_blocks,
        payment_count=payment_count,
        loan_type=loan_type,
        counterparty=counterparty,
        withdraw_signature=withdraw_signature,
        withdraw_counter=withdraw_counter,
        withdraw_amount=withdraw_amount,
        payer=payer,
        expr_list_id=expr_list_id,
        program_hash=program_hash,
        limit=limit,
        duration=duration,
        price=price,
        expiry=expiry,
        offer_refs=offer_refs,
    )

    data = _build_data_expr(
        code=code,
        data=data,
        loan_transaction_id=loan_transaction_id,
        payment_interval_blocks=payment_interval_blocks,
        payment_count=payment_count,
        loan_type=loan_type,
        counterparty=counterparty,
        new_withdrawal_window=new_withdrawal_window,
        withdraw_signature=withdraw_signature,
        withdraw_counter=withdraw_counter,
        withdraw_amount=withdraw_amount,
        channel_close_op=channel_close_op,
        expr_list_id=expr_list_id,
        program_hash=program_hash,
        limit=limit,
        duration=duration,
        price=price,
        expiry=expiry,
        offer_refs=offer_refs,
    )

    tx = Transaction(
        chain_id=chain_id,
        amount=amount,
        code=code,
        counter=counter,
        cost_limit=cost_limit,
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
    payment_interval_blocks: Optional[int] = None,
    payment_count: Optional[int] = None,
    loan_type: Union[int, LoanType] = LoanType.SECURED,
    counterparty: Optional[bytes] = None,
    withdraw_signature: Optional[bytes] = None,
    withdraw_counter: Optional[int] = None,
    withdraw_amount: Optional[int] = None,
    payer: Optional[bytes] = None,
    expr_list_id: Optional[bytes] = None,
    program_hash: Optional[bytes] = None,
    limit: Optional[int] = None,
    duration: Optional[int] = None,
    price: Optional[int] = None,
    expiry: Optional[int] = None,
    offer_refs: Optional[Sequence[Tuple[bytes, bytes]]] = None,
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
            if payment_interval_blocks is None or payment_interval_blocks <= 0:
                raise ValueError("TREASURY_BORROW requires payment_interval_blocks > 0")
            if payment_count is None or payment_count <= 0:
                raise ValueError("TREASURY_BORROW requires payment_count > 0")
            if LoanType(loan_type) == LoanType.UNSECURED:
                if not offer_refs:
                    raise ValueError(
                        "TREASURY_BORROW with loan_type=UNSECURED requires offer_refs"
                    )
                seen = set()
                for seller, offer_transaction_id in offer_refs:
                    if len(seller) != RECIPIENT_SIZE or len(offer_transaction_id) != LOAN_TRANSACTION_ID_SIZE:
                        raise ValueError(
                            "TREASURY_BORROW offer_refs entries must be (32-byte seller, 32-byte offer_transaction_id)"
                        )
                    key = (seller, offer_transaction_id)
                    if key in seen:
                        raise ValueError("TREASURY_BORROW offer_refs must not contain duplicates")
                    seen.add(key)

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

        case TransactionCode.TREASURY_SELL:
            if limit is None or limit <= 0:
                raise ValueError("TREASURY_SELL requires limit > 0")
            if duration is None or duration <= 0 or (duration & (duration - 1)) != 0:
                raise ValueError("TREASURY_SELL requires duration > 0 and a power of 2")
            if price is None or price < 0:
                raise ValueError("TREASURY_SELL requires price >= 0")
            if expiry is None:
                raise ValueError("TREASURY_SELL requires expiry")

        case TransactionCode.STORAGE_CREATE:
            if expr_list_id is None or len(expr_list_id) != EXPR_LIST_ID_SIZE:
                raise ValueError("STORAGE_CREATE requires expr_list_id (32 bytes)")

        case TransactionCode.CODE_ACCOUNT_CREATE:
            if program_hash is None or len(program_hash) != PROGRAM_HASH_SIZE:
                raise ValueError("CODE_ACCOUNT_CREATE requires program_hash (32 bytes)")

        case TransactionCode.CODE_ACCOUNT_CALL:
            if recipient is None:
                raise ValueError("CODE_ACCOUNT_CALL requires recipient")


def _offer_refs_to_expr(offer_refs: Optional[Sequence[Tuple[bytes, bytes]]]) -> Expr:
    """Encode a borrow's claimed-offer refs as a nested `Expr` link-list.

    Each entry is a 2-field sub-list: ``[seller_address,
    offer_transaction_id]``, both stored as bare hash-carrying `link` nodes
    (the same convention used elsewhere in this module for 32-byte ids).

    Args:
        offer_refs: The `(seller_address, offer_transaction_id)` pairs to
            encode, in claim order.

    Returns:
        `NIL` if *offer_refs* is empty/`None`, otherwise a `link`-list
        `Expr` of the encoded entries.
    """
    if not offer_refs:
        return NIL
    result: Expr = NIL
    for seller, offer_transaction_id in reversed(list(offer_refs)):
        entry: Expr = link(
            Expr("link", head_hash=seller),
            link(Expr("link", head_hash=offer_transaction_id), NIL),
        )
        result = link(entry, result)
    return result


def _build_data_expr(
    *,
    code: TransactionCode,
    data: Expr,
    loan_transaction_id: Optional[bytes] = None,
    payment_interval_blocks: Optional[int] = None,
    payment_count: Optional[int] = None,
    loan_type: Union[int, LoanType] = LoanType.SECURED,
    counterparty: Optional[bytes] = None,
    new_withdrawal_window: Optional[int] = None,
    withdraw_signature: Optional[bytes] = None,
    withdraw_counter: Optional[int] = None,
    withdraw_amount: Optional[int] = None,
    channel_close_op: bool = False,
    expr_list_id: Optional[bytes] = None,
    program_hash: Optional[bytes] = None,
    limit: Optional[int] = None,
    duration: Optional[int] = None,
    price: Optional[int] = None,
    expiry: Optional[int] = None,
    offer_refs: Optional[Sequence[Tuple[bytes, bytes]]] = None,
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
            return link(
                int_(LoanType(loan_type)),
                link(
                    int_(payment_interval_blocks),  # type: ignore[arg-type]
                    link(
                        int_(payment_count),  # type: ignore[arg-type]
                        link(_offer_refs_to_expr(offer_refs), NIL),
                    ),
                ),
            )

        case TransactionCode.TREASURY_REPAY | TransactionCode.TREASURY_CLOSE:
            return link(Expr("link", head_hash=loan_transaction_id), NIL)

        case TransactionCode.TREASURY_SELL:
            return link(
                int_(limit),  # type: ignore[arg-type]
                link(
                    int_(duration),  # type: ignore[arg-type]
                    link(
                        int_(price),  # type: ignore[arg-type]
                        link(int_(expiry), NIL),  # type: ignore[arg-type]
                    ),
                ),
            )

        case TransactionCode.STORAGE_CREATE:
            return link(Expr("link", head_hash=expr_list_id), NIL)

        case TransactionCode.CODE_ACCOUNT_CREATE:
            return link(Expr("link", head_hash=program_hash), NIL)

        case TransactionCode.STORAGE_PAYMENT | TransactionCode.CODE_ACCOUNT_CALL:
            return data

    return data
