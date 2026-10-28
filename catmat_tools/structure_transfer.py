"""Lossless structure transport to Multiwfn/CP2K (stdlib only, Angstrom)."""
import math
import re


def validate_cell(cell):
    if not isinstance(cell, (list, tuple)) or len(cell) != 3 or any(not isinstance(v, (list, tuple)) or len(v) != 3 for v in cell):
        raise ValueError("晶胞必须是 3×3 矩阵（Å）")
    cell = [[float(x) for x in v] for v in cell]
    if not all(math.isfinite(x) for v in cell for x in v):
        raise ValueError("晶胞含非有限数值")
    a, b, c = cell
    det = sum(a[i] * (b[(i+1)%3]*c[(i+2)%3] - b[(i+2)%3]*c[(i+1)%3]) for i in range(3))
    if abs(det) < 1e-8:
        raise ValueError("晶胞体积为零或退化")
    return cell


def normalize_pbc(value):
    if isinstance(value, (list, tuple)) and len(value) == 3:
        if any(type(x) is not bool for x in value):
            raise ValueError("周期性数组必须是三个布尔值，不能使用字符串")
        return ''.join(c for c, on in zip('xyz', value) if on) or 'none'
    value = str(value).lower().strip()
    if value not in ('none', 'x', 'y', 'z', 'xy', 'xz', 'yz', 'xyz'):
        raise ValueError("周期性必须为 none/x/y/z/xy/xz/yz/xyz")
    return value


def structure_xyz(structure, comment='CatMat'):
    cell = validate_cell(structure['cell'])
    symbols = structure['symbols']
    # The scaled coordinates and cell are the platform's authoritative pair.
    scaled = structure['scaled_positions']
    if not symbols or len(symbols) != len(scaled):
        raise ValueError("原子数与坐标数不一致")
    if any(not isinstance(p, (list, tuple)) or len(p) != 3 for p in scaled):
        raise ValueError("每个原子必须有三个分数坐标")
    cart = [[sum(float(p[j])*cell[j][i] for j in range(3)) for i in range(3)] for p in scaled]
    if not all(math.isfinite(x) for p in cart for x in p):
        raise ValueError("坐标含非有限数值")
    pbc = normalize_pbc(structure.get('pbc', [True]*3))
    header = 'Lattice="%s" Properties=species:S:1:pos:R:3 pbc="%s"' % (
        ' '.join('%.15g' % x for v in cell for x in v), ' '.join('T' if c in pbc and pbc != 'none' else 'F' for c in 'xyz'))
    return '\n'.join([str(len(symbols)), header] + ['%s %.15g %.15g %.15g' % (s, *p) for s, p in zip(symbols, cart)]) + '\n'


def parse_xyz(text, cell=None):
    lines = text.strip().splitlines()
    try:
        n = int(lines[0])
        if n < 1 or len(lines) != n + 2:
            raise ValueError()
        coords = []
        for line in lines[2:]:
            fields = line.split()
            if len(fields) != 4 or not re.fullmatch(r'[A-Z][a-z]?', fields[0]):
                raise ValueError()
            p = [float(x.replace('D', 'E').replace('d', 'e')) for x in fields[1:]]
            if not all(math.isfinite(x) for x in p):
                raise ValueError()
            coords.append((fields[0], p))
    except (IndexError, TypeError, ValueError):
        raise ValueError("结构需为单帧 XYZ：原子数、注释、元素及三个有限笛卡尔坐标（Å）")
    lattice = re.search(r'\bLattice\s*=\s*"([^"]+)"', lines[1], re.I)
    if lattice:
        values = [float(x) for x in lattice[1].split()]
        if len(values) != 9:
            raise ValueError("XYZ Lattice 必须含九个数值")
        embedded = validate_cell([values[i:i+3] for i in (0, 3, 6)])
        if cell is not None and any(abs(x-y) > 1e-8 for a,b in zip(validate_cell(cell),embedded) for x,y in zip(a,b)):
            raise ValueError("请求晶胞与 XYZ Lattice 不一致")
        cell = embedded
    cell = validate_cell(cell) if cell is not None else None
    pbc = re.search(r'\bpbc\s*=\s*"([^"]+)"', lines[1], re.I)
    hint = None
    if pbc:
        values = pbc[1].upper().split()
        if len(values) != 3 or any(x not in ('T','F','TRUE','FALSE','1','0') for x in values):
            raise ValueError("XYZ pbc 必须为三个布尔值")
        hint = normalize_pbc([x in ('T','TRUE','1') for x in values])
    return {'cell': cell, 'coords': coords, 'pbc': hint}


def preserve_cp2k_geometry(inp, geometry, pbc):
    """Replace only SUBSYS geometry, retaining generator KINDs and cell repeats."""
    lines = inp.splitlines(); stack = []; spans = {}
    for i, line in enumerate(lines):
        m = re.match(r'\s*&([\w]+)\b', line)
        if not m:
            continue
        name = m[1].upper()
        if name == 'END':
            if not stack:
                raise ValueError("Multiwfn 输出 SECTION 未配对")
            old, start = stack.pop()
            end_name = line[m.end():].split('#', 1)[0].split('!', 1)[0].strip().upper()
            if end_name and end_name != old:
                raise ValueError("Multiwfn 输出 SECTION 结束标记不匹配")
            if old in ('CELL', 'COORD') and stack and stack[-1][0] == 'SUBSYS':
                if old in spans:
                    raise ValueError("多个 SUBSYS 几何块，不能安全替换")
                spans[old] = (start, i)
        else:
            stack.append((name, i))
    if stack:
        raise ValueError("Multiwfn 输出 SECTION 未闭合")
    if set(spans) != {'CELL', 'COORD'}:
        raise ValueError("Multiwfn 输出缺少 SUBSYS/CELL 或 COORD")
    replacements = []
    for name, (start, end) in spans.items():
        indent = re.match(r'\s*', lines[start])[0]
        if name == 'COORD':
            content = ['UNIT angstrom', 'SCALED F'] + ['%s %.15g %.15g %.15g' % (s, *p) for s,p in geometry['coords']]
        else:
            content = [line.strip() for line in lines[start+1:end]
                       if not re.match(r'\s*(?:A|B|C|ABC|ALPHA_BETA_GAMMA|PERIODIC)\b', line, re.I)]
            if geometry['cell'] is None:
                content = [line.strip() for line in lines[start+1:end] if not re.match(r'\s*PERIODIC\b', line, re.I)]
            else:
                content += ['%s [angstrom] %.15g %.15g %.15g' % (axis, *v) for axis,v in zip('ABC', geometry['cell'])]
            content += ['PERIODIC ' + pbc.upper()]
        replacements.append((start, end, [indent+'&'+name] + [indent+'  '+x for x in content] + [indent+'&END '+name]))
    for start,end,content in sorted(replacements, reverse=True):
        lines[start:end+1] = content
    return '\n'.join(lines) + '\n'
