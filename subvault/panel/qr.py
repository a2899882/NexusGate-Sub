"""Small dependency-free QR Code generator (byte mode, error correction L).

The implementation follows ISO/IEC 18004 placement and Reed-Solomon rules and
supports QR versions 1 through 10, which is sufficient for panel URLs.
"""

from html import escape


# (block count, total codewords per block, data codewords per block), level L.
_RS_BLOCKS = {
    1: ((1, 26, 19),),
    2: ((1, 44, 34),),
    3: ((1, 70, 55),),
    4: ((1, 100, 80),),
    5: ((1, 134, 108),),
    6: ((2, 86, 68),),
    7: ((2, 98, 78),),
    8: ((2, 121, 97),),
    9: ((2, 146, 116),),
    10: ((2, 86, 68), (2, 87, 69)),
}

_ALIGNMENT = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}


def _gf_mul(x: int, y: int) -> int:
    result = 0
    for _ in range(8):
        result ^= x if y & 1 else 0
        y >>= 1
        x = (x << 1) ^ (0x11D if x & 0x80 else 0)
    return result


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * degree
    result[-1] = 1
    root = 1
    for _ in range(degree):
        for index in range(degree):
            result[index] = _gf_mul(result[index], root)
            if index + 1 < degree:
                result[index] ^= result[index + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    divisor = _rs_divisor(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result[0]
        result = result[1:] + [0]
        for index, coefficient in enumerate(divisor):
            result[index] ^= _gf_mul(coefficient, factor)
    return result


def _append_bits(bits: list[int], value: int, length: int) -> None:
    bits.extend((value >> shift) & 1 for shift in range(length - 1, -1, -1))


def _choose_version(data_length: int) -> int:
    for version, groups in _RS_BLOCKS.items():
        capacity = sum(count * data for count, _, data in groups) * 8
        char_bits = 8 if version <= 9 else 16
        if 4 + char_bits + data_length * 8 <= capacity:
            return version
    raise ValueError("二维码内容过长，请缩短域名或链接")


def _codewords(text: str, version: int) -> list[int]:
    payload = text.encode("utf-8")
    groups = _RS_BLOCKS[version]
    data_capacity = sum(count * data for count, _, data in groups)
    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)  # Byte mode.
    _append_bits(bits, len(payload), 8 if version <= 9 else 16)
    for byte in payload:
        _append_bits(bits, byte, 8)
    bits.extend([0] * min(4, data_capacity * 8 - len(bits)))
    bits.extend([0] * (-len(bits) % 8))
    data = [sum(bits[index + bit] << (7 - bit) for bit in range(8)) for index in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    while len(data) < data_capacity:
        data.append(pad[(len(data) - (len(bits) // 8)) % 2])

    blocks: list[list[int]] = []
    ecc_blocks: list[list[int]] = []
    offset = 0
    for count, total_count, data_count in groups:
        for _ in range(count):
            block = data[offset : offset + data_count]
            offset += data_count
            blocks.append(block)
            ecc_blocks.append(_rs_remainder(block, total_count - data_count))
    result: list[int] = []
    for index in range(max(map(len, blocks))):
        result.extend(block[index] for block in blocks if index < len(block))
    for index in range(max(map(len, ecc_blocks))):
        result.extend(block[index] for block in ecc_blocks if index < len(block))
    return result


def _finder(matrix, row: int, col: int) -> None:
    size = len(matrix)
    for dy in range(-1, 8):
        for dx in range(-1, 8):
            y, x = row + dy, col + dx
            if 0 <= y < size and 0 <= x < size:
                inside = 0 <= dx <= 6 and 0 <= dy <= 6
                matrix[y][x] = bool(inside and (dx in (0, 6) or dy in (0, 6) or (2 <= dx <= 4 and 2 <= dy <= 4)))


def _alignment(matrix, row: int, col: int) -> None:
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            matrix[row + dy][col + dx] = max(abs(dx), abs(dy)) != 1


def _format_bits(matrix, mask: int) -> None:
    # Error correction level L has format bits 01.
    data = (1 << 3) | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    bits = ((data << 10) | remainder) ^ 0x5412
    size = len(matrix)
    bit = lambda index: bool((bits >> index) & 1)
    for index in range(6):
        matrix[index][8] = bit(index)
    matrix[7][8] = bit(6)
    matrix[8][8] = bit(7)
    matrix[8][7] = bit(8)
    for index in range(9, 15):
        matrix[8][14 - index] = bit(index)
    for index in range(8):
        matrix[8][size - 1 - index] = bit(index)
    for index in range(8, 15):
        matrix[size - 15 + index][8] = bit(index)
    matrix[size - 8][8] = True


def _version_bits(matrix, version: int) -> None:
    if version < 7:
        return
    remainder = version
    for _ in range(12):
        remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
    bits = (version << 12) | remainder
    size = len(matrix)
    for index in range(18):
        value = bool((bits >> index) & 1)
        a, b = size - 11 + index % 3, index // 3
        matrix[b][a] = value
        matrix[a][b] = value


def _mask(mask: int, row: int, col: int) -> bool:
    rules = (
        (row + col) % 2 == 0,
        row % 2 == 0,
        col % 3 == 0,
        (row + col) % 3 == 0,
        (row // 2 + col // 3) % 2 == 0,
        (row * col) % 2 + (row * col) % 3 == 0,
        ((row * col) % 2 + (row * col) % 3) % 2 == 0,
        ((row + col) % 2 + (row * col) % 3) % 2 == 0,
    )
    return rules[mask]


def _matrix(version: int, codewords: list[int], mask: int) -> list[list[bool]]:
    size = version * 4 + 17
    matrix = [[None for _ in range(size)] for _ in range(size)]
    _finder(matrix, 0, 0)
    _finder(matrix, 0, size - 7)
    _finder(matrix, size - 7, 0)
    for index in range(8, size - 8):
        if matrix[6][index] is None:
            matrix[6][index] = index % 2 == 0
        if matrix[index][6] is None:
            matrix[index][6] = index % 2 == 0
    positions = _ALIGNMENT[version]
    for row in positions:
        for col in positions:
            if matrix[row][col] is None:
                _alignment(matrix, row, col)
    _format_bits(matrix, mask)
    _version_bits(matrix, version)

    data_bits = [(byte >> shift) & 1 for byte in codewords for shift in range(7, -1, -1)]
    bit_index = 0
    upward = True
    right = size - 1
    while right >= 1:
        if right == 6:
            right -= 1
        for vertical in range(size):
            row = size - 1 - vertical if upward else vertical
            for offset in range(2):
                col = right - offset
                if matrix[row][col] is None:
                    value = data_bits[bit_index] if bit_index < len(data_bits) else 0
                    bit_index += 1
                    matrix[row][col] = bool(value) ^ _mask(mask, row, col)
        upward = not upward
        right -= 2
    return matrix


def _penalty(matrix: list[list[bool]]) -> int:
    size = len(matrix)
    score = 0
    for line in matrix + [list(column) for column in zip(*matrix)]:
        run_color, run_length = line[0], 1
        for value in line[1:] + [None]:
            if value == run_color:
                run_length += 1
            else:
                if run_length >= 5:
                    score += 3 + run_length - 5
                run_color, run_length = value, 1
        pattern = "".join("1" if value else "0" for value in line)
        score += 40 * (pattern.count("00001011101") + pattern.count("10111010000"))
    for row in range(size - 1):
        for col in range(size - 1):
            value = matrix[row][col]
            if matrix[row][col + 1] == value and matrix[row + 1][col] == value and matrix[row + 1][col + 1] == value:
                score += 3
    dark = sum(sum(row) for row in matrix)
    score += (abs(dark * 100 - size * size * 50) // (size * size * 5)) * 10
    return score


def make_qr(text: str) -> list[list[bool]]:
    payload = text.encode("utf-8")
    version = _choose_version(len(payload))
    codewords = _codewords(text, version)
    candidates = [_matrix(version, codewords, mask) for mask in range(8)]
    return min(candidates, key=_penalty)


def qr_svg(text: str, title: str = "订阅二维码", scale: int = 8, border: int = 4) -> bytes:
    matrix = make_qr(text)
    size = len(matrix) + border * 2
    path = []
    for row, values in enumerate(matrix):
        for col, value in enumerate(values):
            if value:
                path.append(f"M{col + border},{row + border}h1v1h-1z")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size * scale}" height="{size * scale}" shape-rendering="crispEdges" role="img">'
        f'<title>{escape(title)}</title><rect width="100%" height="100%" fill="#fff"/>'
        f'<path d="{"".join(path)}" fill="#111827"/></svg>'
    )
    return svg.encode("utf-8")
