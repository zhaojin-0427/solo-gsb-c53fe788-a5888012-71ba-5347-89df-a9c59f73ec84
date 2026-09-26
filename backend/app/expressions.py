"""Z3 支撑的小型表达式语言。

语法 (EBNF)::

    expr   := or
    or     := and ('or' and)*
    and    := not ('and' not)*
    not    := 'not' not | cmp
    cmp    := arith (('=' | '!=' | '<' | '<=' | '>' | '>=') arith)?
    arith  := term (('+' | '-') term)*
    term   := factor (('*' | '/' | '%') factor)*
    factor := '-' factor | atom
    atom   := 数字 | 'true' | 'false' | 标识符 [ '(' expr ')' ]
           |  '(' expr ')'

标识符要么是布尔/整数变量，要么是内置函数：
    succ(act)          活动成功次数 (int)
    effects(key)       外部效果 key 的生效次数 (int)
    undone(key)        外部效果 key 的撤销次数 (int)

类型规则：布尔表达式可作为前置条件/守卫/不变量；算术表达式可作为赋值右侧。
解析结果是一组可散列、可重入的 AST 节点；Z3 仅在可行性检查（compile_z3）时按需构造。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import z3

# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------

_TOKEN_KEYWORDS = {"and", "or", "not", "true", "false"}


@dataclass(frozen=True)
class Token:
    kind: str  # NUM STR ID KW OP LP RP
    value: str
    pos: int


def _tokenize(src: str) -> list[Token]:
    toks: list[Token] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit():
            j = i
            while j < n and src[j].isdigit():
                j += 1
            toks.append(Token("NUM", src[i:j], i))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            toks.append(Token("KW" if word in _TOKEN_KEYWORDS else "ID", word, i))
            i = j
            continue
        if c in "\"'":
            quote = c
            j = i + 1
            buf: list[str] = []
            while j < n and src[j] != quote:
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j + 1])
                    j += 2
                else:
                    buf.append(src[j])
                    j += 1
            if j >= n:
                raise ExpressionError("字符串缺少结束引号", i)
            toks.append(Token("STR", "".join(buf), i))
            i = j + 1
            continue
        if src.startswith("!=", i) or src.startswith("<=", i) or src.startswith(">=", i):
            toks.append(Token("OP", src[i:i + 2], i))
            i += 2
            continue
        if c in "+-*/%<>=()":
            toks.append(Token("LP" if c == "(" else "RP" if c == ")" else "OP", c, i))
            i += 1
            continue
        raise ExpressionError(f"无法识别的字符 {c!r}（位置 {i}）", i)
    toks.append(Token("EOF", "", n))
    return toks


# ---------------------------------------------------------------------------
# AST 节点
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntLit:
    value: int


@dataclass(frozen=True)
class BoolLit:
    value: bool


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class StrLit:
    value: str


@dataclass(frozen=True)
class Call:
    name: str
    arg: "Node"
    pos: int


@dataclass(frozen=True)
class Unary:
    op: str  # '-' / 'not'
    operand: "Node"
    pos: int


@dataclass(frozen=True)
class Binary:
    op: str
    left: "Node"
    right: "Node"
    pos: int


Node = IntLit | BoolLit | StrLit | Var | Call | Unary | Binary

_BOOL_OPS = {"and", "or"}
_CMP_OPS = {"=", "!=", "<", "<=", ">", ">="}
_ARITH_OPS = {"+", "-", "*", "/", "%"}
_Z3_BOOL_FUNCS = {"succ", "effects", "undone"}


class ExpressionError(Exception):
    def __init__(self, message: str, pos: int | None = None):
        super().__init__(message if pos is None else f"{message}（位置 {pos}）")
        self.pos = pos


# ---------------------------------------------------------------------------
# 递归下降解析
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = _tokenize(src)
        self.i = 0

    def _peek(self) -> Token:
        return self.toks[self.i]

    def _next(self) -> Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def parse(self) -> Node:
        node = self._parse_or()
        if self._peek().kind != "EOF":
            t = self._peek()
            raise ExpressionError(f"多余的符号 {t.value!r}", t.pos)
        return node

    def _parse_or(self) -> Node:
        node = self._parse_and()
        while self._peek().kind == "KW" and self._peek().value == "or":
            op = self._next()
            rhs = self._parse_and()
            node = Binary("or", node, rhs, op.pos)
        return node

    def _parse_and(self) -> Node:
        node = self._parse_not()
        while self._peek().kind == "KW" and self._peek().value == "and":
            op = self._next()
            rhs = self._parse_not()
            node = Binary("and", node, rhs, op.pos)
        return node

    def _parse_not(self) -> Node:
        t = self._peek()
        if t.kind == "KW" and t.value == "not":
            self._next()
            return Unary("not", self._parse_not(), t.pos)
        return self._parse_cmp()

    def _parse_cmp(self) -> Node:
        node = self._parse_arith()
        t = self._peek()
        if t.kind == "OP" and t.value in _CMP_OPS:
            self._next()
            rhs = self._parse_arith()
            node = Binary(t.value, node, rhs, t.pos)
            if self._peek().kind == "OP" and self._peek().value in _CMP_OPS:
                t2 = self._peek()
                raise ExpressionError("不允许连续比较，请用 and 连接", t2.pos)
        return node

    def _parse_arith(self) -> Node:
        node = self._parse_term()
        while self._peek().kind == "OP" and self._peek().value in {"+", "-"}:
            op = self._next()
            node = Binary(op.value, node, self._parse_term(), op.pos)
        return node

    def _parse_term(self) -> Node:
        node = self._parse_factor()
        while self._peek().kind == "OP" and self._peek().value in {"*", "/", "%"}:
            op = self._next()
            node = Binary(op.value, node, self._parse_factor(), op.pos)
        return node

    def _parse_factor(self) -> Node:
        t = self._peek()
        if t.kind == "OP" and t.value == "-":
            self._next()
            return Unary("-", self._parse_factor(), t.pos)
        if t.kind == "OP" and t.value == "+":
            self._next()
            return self._parse_factor()
        return self._parse_atom()

    def _parse_atom(self) -> Node:
        t = self._peek()
        if t.kind == "NUM":
            self._next()
            return IntLit(int(t.value))
        if t.kind == "KW" and t.value in {"true", "false"}:
            self._next()
            return BoolLit(t.value == "true")
        if t.kind == "ID":
            self._next()
            if self._peek().kind == "LP":
                lp = self._next()
                arg = self._parse_or()
                rp = self._peek()
                if rp.kind != "RP":
                    raise ExpressionError("函数调用缺少右括号 ')'", lp.pos)
                self._next()
                if t.value not in _Z3_BOOL_FUNCS:
                    raise ExpressionError(f"未知函数 {t.value}，可用：{', '.join(sorted(_Z3_BOOL_FUNCS))}", t.pos)
                if not isinstance(arg, StrLit):
                    raise ExpressionError(f"{t.value}() 的参数必须是引号字符串，如 {t.value}(\"key\")", t.pos)
                return Call(t.value, arg, t.pos)
            return Var(t.value)
        if t.kind == "STR":
            self._next()
            return StrLit(t.value)
        if t.kind == "LP":
            self._next()
            node = self._parse_or()
            rp = self._peek()
            if rp.kind != "RP":
                raise ExpressionError("缺少右括号 ')'", t.pos)
            self._next()
            return node
        raise ExpressionError(f"此处不应出现 {t.value or '表达式结尾'}", t.pos)


def parse(src: str) -> Node:
    if src is None or not src.strip():
        raise ExpressionError("表达式为空")
    return _Parser(src).parse()


# ---------------------------------------------------------------------------
# 具体求值（用于前置条件 / 守卫 / 赋值），context 见 workflow.py
# ---------------------------------------------------------------------------

class EvalError(ExpressionError):
    pass


def _type_error(msg: str, node: Node):
    pos = getattr(node, "pos", None)
    raise EvalError(msg, pos)


def _z3_boolfunc_value(name: str, arg: Any, ctx: Mapping[str, Any]) -> int:
    if not isinstance(arg, str):
        raise EvalError(f"{name}() 的参数必须是用引号给出的效果键/活动名")
    if name == "succ":
        return int(ctx.get("succ", {}).get(arg, 0))
    if name == "effects":
        return int(ctx.get("effects", {}).get(arg, 0))
    return int(ctx.get("undone", {}).get(arg, 0))


def eval_node(node: Node, variables: Mapping[str, Any], functions: Mapping[str, Any] | None = None) -> Any:
    """在具体变量赋值上求值；functions: {函数名: callable(arg)->Any}（供检验器使用）。"""
    if isinstance(node, IntLit):
        return node.value
    if isinstance(node, BoolLit):
        return node.value
    if isinstance(node, Var):
        if node.name not in variables:
            raise EvalError(f"未知变量 {node.name}（请先在变量表中声明）")
        return variables[node.name]
    if isinstance(node, StrLit):
        return node.value
    if isinstance(node, Call):
        arg = eval_node(node.arg, variables, functions)
        if not isinstance(arg, str):
            raise EvalError(f"{node.name}() 的参数必须是引号字符串")
        if functions is not None and node.name in functions:
            return functions[node.name](arg)
        return _z3_boolfunc_value(node.name, arg, functions or {})
    if isinstance(node, Unary):
        v = eval_node(node.operand, variables, functions)
        if node.op == "not":
            if not isinstance(v, bool):
                _type_error("not 只能作用于布尔表达式", node)
            return not v
        if not isinstance(v, int) or isinstance(v, bool):
            _type_error("一元负号只能作用于整数", node)
        return -v
    if isinstance(node, Binary):
        if node.op in _BOOL_OPS:
            l = eval_node(node.left, variables, functions)
            if not isinstance(l, bool):
                _type_error(f"{node.op} 两侧必须是布尔值", node.left)
            r = eval_node(node.right, variables, functions)
            if not isinstance(r, bool):
                _type_error(f"{node.op} 两侧必须是布尔值", node.right)
            return (l and r) if node.op == "and" else (l or r)
        l = eval_node(node.left, variables, functions)
        r = eval_node(node.right, variables, functions)
        if node.op in _CMP_OPS:
            if isinstance(l, bool) != isinstance(r, bool):
                _type_error("不能跨布尔与整数比较", node)
            if node.op == "=":
                return l == r
            if node.op == "!=":
                return l != r
            if isinstance(l, bool):
                _type_error("布尔值只能用 = / != 比较", node)
            return {"<": l < r, "<=": l <= r, ">": l > r, ">=": l >= r}[node.op]
        if isinstance(l, bool) or isinstance(r, bool):
            _type_error(f"算术运算 {node.op} 不能作用于布尔值", node)
        if node.op == "+":
            return l + r
        if node.op == "-":
            return l - r
        if node.op == "*":
            return l * r
        if r == 0:
            raise EvalError("除数为零", node.pos)
        # 向零取整，与 C 语言 % 语义一致，也符合 z3 的 truncation 预期
        q = abs(l) // abs(r) * (1 if (l < 0) == (r < 0) else -1)
        if node.op == "/":
            return q
        return l - q * r
    raise EvalError("内部错误：未知 AST 节点")  # pragma: no cover


def eval_bool(src_or_node, variables, functions=None) -> bool:
    node = parse(src_or_node) if isinstance(src_or_node, str) else src_or_node
    val = eval_node(node, variables, functions)
    if not isinstance(val, bool):
        raise EvalError("此处要求布尔表达式（前置条件 / 守卫 / 不变量）")
    return val


def eval_assign(src_or_node, variables) -> Any:
    node = parse(src_or_node) if isinstance(src_or_node, str) else src_or_node
    val = eval_node(node, variables)
    return val


def references(node: Node) -> tuple[set[str], set[str]]:
    """返回 (引用的变量名集合, 引用的函数名集合)。"""
    vars_, funcs = set(), set()
    stack = [node]
    while stack:
        x = stack.pop()
        if isinstance(x, Var):
            vars_.add(x.name)
        elif isinstance(x, Call):
            funcs.add(x.name)
            stack.append(x.arg)
        elif isinstance(x, Unary):
            stack.append(x.operand)
        elif isinstance(x, Binary):
            stack.extend((x.left, x.right))
    return vars_, funcs


# ---------------------------------------------------------------------------
# Z3 编译与可行性检查
# ---------------------------------------------------------------------------

def infer_type(node: Node, var_types: Mapping[str, str]) -> str:
    """根据变量类型声明（'int' / 'bool'）推断表达式类型。"""
    if isinstance(node, (IntLit, Call)):
        return "int"
    if isinstance(node, BoolLit):
        return "bool"
    if isinstance(node, StrLit):
        raise ExpressionError("字符串只能作为 succ/effects/undone 的参数", getattr(node, "pos", None))
    if isinstance(node, Var):
        if node.name not in var_types:
            raise ExpressionError(f"未知变量 {node.name}（请先在变量表中声明）", getattr(node, "pos"))
        return var_types[node.name]
    if isinstance(node, Unary):
        t = infer_type(node.operand, var_types)
        if node.op == "not":
            if t != "bool":
                _type_error("not 只能作用于布尔表达式", node)
            return "bool"
        if t != "int":
            _type_error("一元负号只能作用于整数", node)
        return "int"
    if isinstance(node, Binary):
        lt = infer_type(node.left, var_types)
        rt = infer_type(node.right, var_types)
        if node.op in _BOOL_OPS:
            if lt != "bool" or rt != "bool":
                _type_error(f"{node.op} 两侧必须是布尔表达式", node)
            return "bool"
        if lt != rt:
            _type_error("比较/算术两侧类型必须一致", node)
        if node.op in _CMP_OPS:
            if node.op in {"=", "!="} and lt in {"int", "bool"}:
                return "bool"
            if lt != "int":
                _type_error("序关系只能用于整数", node)
            return "bool"
        if lt != "int":
            _type_error("算术运算只能用于整数", node)
        return "int"
    raise ExpressionError("内部错误")  # pragma: no cover


def compile_z3(node: Node, var_types: Mapping[str, str], ctx_symbols=None):
    """把 AST 编译成 Z3 表达式。

    ctx_symbols: 可选 {名字: z3 常量}，用于把具体上下文（如 succ_count）符号化。
    未给出时，变量按 var_types 生成自由常量；Call 参数目前仅接受字符串字面量。
    """
    ctx_symbols = ctx_symbols or {}

    def conv(n: Node):
        if isinstance(n, IntLit):
            return z3.IntVal(n.value)
        if isinstance(n, BoolLit):
            return z3.BoolVal(n.value)
        if isinstance(n, StrLit):
            raise ExpressionError("字符串只能作为 succ/effects/undone 的参数")
        if isinstance(n, Var):
            if n.name in ctx_symbols:
                return ctx_symbols[n.name]
            return z3.Bool(n.name) if var_types.get(n.name) == "bool" else z3.Int(n.name)
        if isinstance(n, Call):
            if not isinstance(n.arg, StrLit):  # 解析阶段已保证
                raise ExpressionError(f"{n.name}() 的参数必须是引号字符串", n.pos)
            symbol_name = f"{n.name}__{n.arg.value}"
            if symbol_name in ctx_symbols:
                return ctx_symbols[symbol_name]
            return z3.Int(symbol_name)
        if isinstance(n, Unary):
            v = conv(n.operand)
            return z3.Not(v) if n.op == "not" else -v
        if isinstance(n, Binary):
            l, r = conv(n.left), conv(n.right)
            table = {
                "and": lambda: z3.And(l, r),
                "or": lambda: z3.Or(l, r),
                "=": lambda: l == r,
                "!=": lambda: l != r,
                "<": lambda: l < r,
                "<=": lambda: l <= r,
                ">": lambda: l > r,
                ">=": lambda: l >= r,
                "+": lambda: l + r,
                "-": lambda: l - r,
                "*": lambda: l * r,
                "/": lambda: l / r,
                "%": lambda: l % r,
            }
            return table[n.op]()
        raise ExpressionError("内部错误")  # pragma: no cover

    return conv(node)


def satisfiable(node: Node, var_types: Mapping[str, str], timeout_ms: int = 2000) -> str:
    """可行性检查，返回 'sat' / 'unsat' / 'unknown'。

    用于前置条件静态排查：若某活动的前置条件在任何计数取值下都不可满足，
    说明该活动永远不可达，提示用户建模可能有误。
    """
    z3expr = compile_z3(node, var_types)
    solver = z3.Solver()
    solver.set("timeout", timeout_ms)
    solver.add(z3expr)
    # 为所有 succ/effects/undone 整数符号加上非负约束
    for const in _walk_consts(z3expr):
        if "__" in str(const) and const.sort() == z3.IntSort():
            solver.add(const >= 0)
    res = solver.check()
    return str(res).lower()


def _walk_consts(expr):
    seen, out = set(), []
    stack = [expr]
    while stack:
        e = stack.pop()
        if z3.is_const(e) and not z3.is_int_value(e) and not z3.is_true(e) and not z3.is_false(e):
            name = str(e)
            if name not in seen:
                seen.add(name)
                out.append(e)
        else:
            stack.extend(e.children())
    return out
