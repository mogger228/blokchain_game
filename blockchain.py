"""
blockchain.py — логика одного блока и работа с цепочкой блоков.

Это учебная реализация. Блок хранит перевод "от кого -> кому -> сколько"
и хеш предыдущего блока, благодаря чему блоки связаны в цепочку.
"""

import hashlib


class Block:
    """
    Один блок цепочки.

    Поля:
        index         — порядковый номер блока (0 — генезис-блок)
        timestamp     — время создания блока (строка)
        sender        — отправитель
        recipient     — получатель
        amount        — сумма перевода
        previous_hash — хеш предыдущего блока в цепочке
        hash          — собственный хеш этого блока (считается от всех полей выше)
    """

    def __init__(self, index, timestamp, sender, recipient, amount, previous_hash, hash_=None):
        self.index = index
        self.timestamp = timestamp
        self.sender = sender
        self.recipient = recipient
        # Приводим сумму к float сразу: SQLite хранит amount как REAL и при
        # чтении всегда вернёт float, поэтому хеш нужно считать единообразно
        # и при создании блока, и при его чтении из базы (иначе 0 != 0.0).
        self.amount = float(amount)
        self.previous_hash = previous_hash
        # Если хеш не передали явно (например, при чтении из базы) — считаем его сами.
        self.hash = hash_ if hash_ is not None else self.calculate_hash()

    def calculate_hash(self):
        """
        Считает sha256-хеш блока на основе всех его полей, включая previous_hash.
        Именно этот механизм связывает блоки в цепочку: если поменять любое
        поле блока, его хеш изменится, и цепочка перестанет быть валидной.
        """
        # Собираем все поля блока в одну строку в фиксированном порядке.
        block_string = (
            f"{self.index}"
            f"{self.timestamp}"
            f"{self.sender}"
            f"{self.recipient}"
            f"{self.amount}"
            f"{self.previous_hash}"
        )
        # Переводим строку в байты и считаем sha256.
        return hashlib.sha256(block_string.encode("utf-8")).hexdigest()


def create_genesis_block():
    """
    Создаёт самый первый блок цепочки — генезис-блок.
    У него нет предыдущего блока, поэтому previous_hash = "0",
    а перевод условный: отправитель и получатель "system", сумма 0.
    """
    import datetime

    return Block(
        index=0,
        timestamp=str(datetime.datetime.now()),
        sender="system",
        recipient="system",
        amount=0,
        previous_hash="0",
    )


def create_new_block(last_block, sender, recipient, amount):
    """
    Создаёт новый блок-перевод, который продолжает цепочку после last_block.
    previous_hash нового блока — это хеш последнего блока в цепочке.
    """
    import datetime

    return Block(
        index=last_block.index + 1,
        timestamp=str(datetime.datetime.now()),
        sender=sender,
        recipient=recipient,
        amount=amount,
        previous_hash=last_block.hash,
    )


def calculate_balance(blocks, user):
    """
    Считает баланс пользователя по всей истории цепочки:
    баланс = сумма всех полученных переводов минус сумма всех отправленных.

    Параметр blocks — список объектов Block (обычно вся цепочка из базы).
    Параметр user — имя пользователя, чей баланс нужно посчитать.

    Генезис-блок на баланс не влияет, так как в нём sender = recipient = "system"
    и amount = 0.
    """
    balance = 0.0
    for block in blocks:
        if block.recipient == user:
            balance += block.amount
        if block.sender == user:
            balance -= block.amount
    return balance


def validate_chain(blocks):
    """
    Проверяет целостность всей цепочки блоков.

    Для каждого блока проверяются два условия:
      1) сохранённый hash блока совпадает с хешем, пересчитанным от его полей
         (это значит, что данные блока никто не подменил вручную);
      2) previous_hash блока совпадает с hash предыдущего блока
         (это значит, что цепочка не разорвана).

    Параметр blocks — список объектов Block, отсортированный по index.

    Возвращает (True, None), если цепочка валидна,
    или (False, index_блока_с_ошибкой), если найдено нарушение.
    """
    for i, block in enumerate(blocks):
        # 1) Проверяем, что хеш блока не подделан.
        recalculated_hash = block.calculate_hash()
        if recalculated_hash != block.hash:
            return False, block.index

        # 2) Проверяем связь с предыдущим блоком (для всех, кроме генезис-блока).
        if i > 0:
            previous_block = blocks[i - 1]
            if block.previous_hash != previous_block.hash:
                return False, block.index

    return True, None
