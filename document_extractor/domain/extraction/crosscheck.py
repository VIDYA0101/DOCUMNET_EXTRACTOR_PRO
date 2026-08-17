"""Arithmetic cross-checks.

These catch what per-field confidence cannot see: a dropped table row, a
merged row, a column misalignment, an OCR digit error. sum(lines) != subtotal
is the single highest-value accuracy signal in the whole system.

Expressions are evaluated by a whitelisted AST walker. eval() on an expression
that arrived inside a template file someone received by email is remote code
execution.
"""
from __future__ import annotations

import ast
import logging
from decimal import Decimal, InvalidOperation

from ..models import CrossCheckResult

log = logging.getLogger(__name__)

MAX_EXPR_LEN = 400

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.And, ast.Or, ast.Not,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.List, ast.Tuple, ast.Attribute, ast.IfExp,
)


def _dsum(values):
    total = Decimal(0)
    for v in values or []:
        d = _to_dec(v)
        if d is not None:
            total += d
    return total


def _to_dec(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


_FUNCS = {
    "abs": lambda x: abs(_to_dec(x) or Decimal(0)),
    "sum": _dsum,
    "min": lambda *a: min(a[0]) if len(a) == 1 and isinstance(a[0], list) else min(a),
    "max": lambda *a: max(a[0]) if len(a) == 1 and isinstance(a[0], list) else max(a),
    "round": lambda x, n=0: round(_to_dec(x) or Decimal(0), int(n)),
    "len": lambda x: len(x or []),
    "coalesce": lambda *a: next((x for x in a if x is not None), None),
}


class _Evaluator(ast.NodeVisitor):
    def __init__(self, names: dict):
        self.names = names

    def visit(self, node):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"disallowed expression node: {type(node).__name__}")
        return super().visit(node)

    def visit_Expression(self, node): return self.visit(node.body)
    def visit_Constant(self, node):
        return _to_dec(node.value) if isinstance(node.value, (int, float)) else node.value

    def visit_Name(self, node):
        if node.id in _FUNCS:
            return _FUNCS[node.id]
        if node.id not in self.names:
            raise KeyError(node.id)
        return self.names[node.id]

    def visit_Attribute(self, node):
        # Only supports the flat "lines.amount" column accessor.
        base = node.value
        if isinstance(base, ast.Name) and base.id == "lines":
            rows = self.names.get("lines", [])
            return [r.get(node.attr) for r in rows]
        raise ValueError("attribute access not permitted")

    def visit_List(self, node): return [self.visit(e) for e in node.elts]
    def visit_Tuple(self, node): return [self.visit(e) for e in node.elts]

    def visit_BinOp(self, node):
        left, right = self.visit(node.left), self.visit(node.right)
        left, right = _to_dec(left) or Decimal(0), _to_dec(right) or Decimal(0)
        op = type(node.op)
        if op is ast.Add: return left + right
        if op is ast.Sub: return left - right
        if op is ast.Mult: return left * right
        if op is ast.Div:
            return Decimal(0) if right == 0 else left / right
        if op is ast.Mod:
            return Decimal(0) if right == 0 else left % right
        if op is ast.Pow: return left ** right
        raise ValueError("operator not permitted")

    def visit_UnaryOp(self, node):
        val = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -(_to_dec(val) or Decimal(0))
        if isinstance(node.op, ast.UAdd):
            return _to_dec(val) or Decimal(0)
        if isinstance(node.op, ast.Not):
            return not val
        raise ValueError("operator not permitted")

    def visit_Compare(self, node):
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            ld, rd = _to_dec(left), _to_dec(right)
            if ld is not None and rd is not None:
                left_cmp, right_cmp = ld, rd
            else:
                left_cmp, right_cmp = left, right
            t = type(op)
            if t is ast.Eq: ok = left_cmp == right_cmp
            elif t is ast.NotEq: ok = left_cmp != right_cmp
            elif t is ast.Lt: ok = left_cmp < right_cmp
            elif t is ast.LtE: ok = left_cmp <= right_cmp
            elif t is ast.Gt: ok = left_cmp > right_cmp
            elif t is ast.GtE: ok = left_cmp >= right_cmp
            else: raise ValueError("comparison not permitted")
            if not ok:
                return False
            left = right
        return True

    def visit_BoolOp(self, node):
        values = [self.visit(v) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)

    def visit_IfExp(self, node):
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError("only whitelisted functions may be called")
        args = [self.visit(a) for a in node.args]
        return _FUNCS[node.func.id](*args)


def evaluate(expression: str, names: dict):
    """Evaluate a whitelisted expression. Raises on anything not permitted."""
    if not expression or len(expression) > MAX_EXPR_LEN:
        raise ValueError("expression missing or too long")
    tree = ast.parse(expression, mode="eval")
    return _Evaluator(names).visit(tree)


def build_namespace(fields: dict, line_items: list) -> dict:
    """Expose typed field values and line-item columns to expressions."""
    ns: dict = {}
    for name, fr in (fields or {}).items():
        ns[name] = fr.value if getattr(fr, "ok", False) else None
    rows = []
    for li in line_items or []:
        rows.append(dict(getattr(li, "cells", {}) or {}))
    ns["lines"] = rows
    ns["line_count"] = len(rows)
    return ns


def run_checks(specs: list[dict], fields: dict, line_items: list
               ) -> list[CrossCheckResult]:
    """Run every cross-check. A check whose inputs are missing is skipped, not failed."""
    ns = build_namespace(fields, line_items)
    results: list[CrossCheckResult] = []

    for spec in specs or []:
        check_id = spec.get("id", "check")
        expr = spec.get("expr", "")
        severity = spec.get("on_fail", "review")
        required = spec.get("requires") or _names_used(expr)
        missing = [n for n in required
                   if n not in ("lines", "line_count") and ns.get(n) is None]
        if missing:
            results.append(CrossCheckResult(
                check_id, True, f"skipped (missing: {', '.join(sorted(missing))})",
                "warn"))
            continue
        try:
            passed = bool(evaluate(expr, ns))
            detail = "" if passed else _explain(expr, ns)
        except Exception as exc:
            log.warning("cross-check %s failed to evaluate: %s", check_id, exc)
            results.append(CrossCheckResult(check_id, True,
                                            f"not evaluated: {exc}", "warn"))
            continue
        results.append(CrossCheckResult(check_id, passed, detail, severity))
    return results


def _names_used(expr: str) -> list[str]:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return []
    return [n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id not in _FUNCS]


def _explain(expr: str, ns: dict) -> str:
    """Show the operand values so the review screen can say *why* it failed."""
    parts = []
    for name in sorted(set(_names_used(expr))):
        if name == "lines":
            continue
        val = ns.get(name)
        if val is not None:
            parts.append(f"{name}={val}")
    if "lines" in expr:
        rows = ns.get("lines", [])
        parts.append(f"line_count={len(rows)}")
    return "; ".join(parts)


DEFAULT_INVOICE_CHECKS = [
    {"id": "row_math",
     "expr": "True", "on_fail": "review"},
    {"id": "line_sum_vs_subtotal",
     "expr": "abs(sum(lines.amount) - subtotal) <= max(1.0, 0.005 * subtotal)",
     "on_fail": "review"},
    {"id": "grand_total",
     "expr": "abs(subtotal + tax_total - grand_total) <= max(1.0, 0.005 * grand_total)",
     "on_fail": "review"},
]
