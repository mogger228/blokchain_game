"""Командный интерфейс учебного блокчейна."""

from __future__ import annotations

import argparse
import sys

import service


def _print_block(block) -> None:
    print(f"Блок #{block.index}")
    print(f"  Время:         {block.timestamp}")
    print(f"  Перевод:       {block.sender} -> {block.recipient}, сумма: {block.amount}")
    print(f"  Hash:          {block.hash}")
    print(f"  Previous hash: {block.previous_hash}")


def cmd_init(_args) -> None:
    created = service.initialize()
    if created:
        print("База данных создана, записан генезис-блок.")
    else:
        print("База данных уже инициализирована, генезис-блок не создаётся повторно.")


def cmd_send(args) -> None:
    service.require_existing_database()
    block = service.create_trusted_transaction(args.sender, args.recipient, args.amount)
    print(f"Новый блок #{block.index} добавлен в цепочку:")
    print(f"  {block.sender} -> {block.recipient}, сумма: {block.amount}")
    print(f"  hash: {block.hash}")
    print(f"  previous_hash: {block.previous_hash}")


def cmd_balance(args) -> None:
    service.require_existing_database()
    print(f"Баланс {args.user}: {service.get_named_balance(args.user)}")


def cmd_chain(_args) -> None:
    service.require_existing_database()
    blocks = service.get_chain()
    if not blocks:
        print("Цепочка пуста.")
        return
    for block in blocks:
        _print_block(block)
        print("-" * 60)


def cmd_validate(_args) -> None:
    service.require_existing_database()
    valid, broken = service.check_chain()
    if valid:
        print("Цепочка валидна.")
    else:
        print(f"Цепочка НЕ валидна! Нарушение целостности на блоке #{broken}.")


def cmd_transaction(args) -> None:
    service.require_existing_database()
    block = service.get_transaction(args.transaction_id)
    if block is None:
        raise service.ValidationError(f"Транзакция с ID {args.transaction_id} не найдена.")
    _print_block(block)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Игрушечный блокчейн — учебный проект на Python.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_init = subparsers.add_parser("init", help="создать базу и генезис-блок")
    parser_init.set_defaults(func=cmd_init)

    parser_send = subparsers.add_parser("send", help="создать новый блок-перевод")
    parser_send.add_argument("--from", dest="sender", required=True, help="отправитель")
    parser_send.add_argument("--to", dest="recipient", required=True, help="получатель")
    parser_send.add_argument("--amount", required=True, help="сумма перевода")
    parser_send.set_defaults(func=cmd_send)

    parser_balance = subparsers.add_parser("balance", help="показать баланс пользователя")
    parser_balance.add_argument("--user", required=True, help="имя пользователя")
    parser_balance.set_defaults(func=cmd_balance)

    parser_chain = subparsers.add_parser("chain", help="вывести всю цепочку блоков")
    parser_chain.set_defaults(func=cmd_chain)

    parser_validate = subparsers.add_parser("validate", help="проверить целостность цепочки")
    parser_validate.set_defaults(func=cmd_validate)

    parser_transaction = subparsers.add_parser("transaction", help="найти транзакцию по ID")
    parser_transaction.add_argument("--id", dest="transaction_id", type=int, required=True)
    parser_transaction.set_defaults(func=cmd_transaction)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
        return 0
    except (service.ServiceError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

