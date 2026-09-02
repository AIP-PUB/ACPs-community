"""位置无关的 Click Group：组选项可以出现在子命令之前或之后。

Click 默认要求组级选项写在子命令名前面，因此
`acps-cli entity derive --mtls-url ...` 会被当成 derive 的未知选项。
FlexibleGroup 在本层 parse_args 之前重排剩余 argv：

- 属于当前组、且未被更深层命令同时声明的选项，提升到子命令前面
- 属于子命令（或更深）的选项，即使写在子命令前面，也会下推到子命令后面
- 同名选项（例如 cert 与 cert eab 都有 --server-url）：写在子命令前归本层，写在后面归深层
- 永不移动 --help / -h，避免 `entity derive --help` 变成 entity 的帮助
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import click

_HELP_OPTION_NAMES = frozenset({"-h", "--help"})


@dataclass(frozen=True)
class _Piece:
    kind: str
    tokens: tuple[str, ...]
    option_name: str | None = None


def _option_names(param: click.Option) -> tuple[str, ...]:
    return tuple(param.opts) + tuple(param.secondary_opts)


def _option_value_arity(param: click.Option) -> int:
    if param.is_flag or param.count:
        return 0
    nargs = param.nargs
    if nargs is None or nargs < 0:
        return 1
    return int(nargs)


def _walk_commands(command: click.Command, ctx: click.Context) -> Iterator[click.Command]:
    seen: set[int] = set()

    def walk(cmd: click.Command) -> Iterator[click.Command]:
        marker = id(cmd)
        if marker in seen:
            return
        seen.add(marker)
        yield cmd
        if isinstance(cmd, click.Group):
            for name in cmd.list_commands(ctx):
                sub = cmd.get_command(ctx, name)
                if sub is not None:
                    yield from walk(sub)

    yield from walk(command)


def _iter_options(command: click.Command) -> Iterator[click.Option]:
    for param in command.params:
        if isinstance(param, click.Option):
            yield param


def _collect_option_index(command: click.Command, ctx: click.Context) -> dict[str, click.Option]:
    index: dict[str, click.Option] = {}
    for cmd in _walk_commands(command, ctx):
        for param in _iter_options(cmd):
            for name in _option_names(param):
                index.setdefault(name, param)
    return index


def _self_option_names(command: click.Command) -> set[str]:
    names: set[str] = set()
    for param in _iter_options(command):
        names.update(name for name in _option_names(param) if name not in _HELP_OPTION_NAMES)
    return names


def _subtree_option_names(command: click.Command, ctx: click.Context) -> set[str]:
    names: set[str] = set()
    for cmd in _walk_commands(command, ctx):
        for param in _iter_options(cmd):
            names.update(name for name in _option_names(param) if name not in _HELP_OPTION_NAMES)
    return names


def _looks_like_option(arg: str) -> bool:
    return arg.startswith("-") and arg != "-"


def _split_option_token(arg: str, option_index: dict[str, click.Option]) -> tuple[str, bool]:
    """返回 (选项名, 是否已在本 token 内带值)。"""
    if arg.startswith("--"):
        if "=" in arg:
            return arg.split("=", 1)[0], True
        return arg, False
    if len(arg) > 2 and "=" in arg:
        return arg.split("=", 1)[0], True
    if len(arg) > 2:
        short_name = arg[:2]
        param = option_index.get(short_name)
        if param is not None and _option_value_arity(param) > 0:
            return short_name, True
    return arg, False


def _tokenize(args: Sequence[str], option_index: dict[str, click.Option]) -> list[_Piece]:
    pieces: list[_Piece] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            pieces.append(_Piece("end", tuple(args[index:])))
            break
        if not _looks_like_option(arg):
            pieces.append(_Piece("pos", (arg,)))
            index += 1
            continue
        name, inline = _split_option_token(arg, option_index)
        tokens = [arg]
        if not inline:
            param = option_index.get(name)
            arity = _option_value_arity(param) if param is not None else 0
            consumed = 0
            while consumed < arity:
                next_index = index + 1 + consumed
                if next_index >= len(args) or args[next_index] == "--":
                    break
                tokens.append(args[next_index])
                consumed += 1
        pieces.append(_Piece("opt", tuple(tokens), option_name=name))
        index += len(tokens)
    return pieces


def _resolve_path(group: click.Group, ctx: click.Context, positionals: Sequence[str]) -> list[click.Command]:
    path: list[click.Command] = []
    current: click.Command = group
    for name in positionals:
        if not isinstance(current, click.Group):
            break
        sub = current.get_command(ctx, name)
        if sub is None:
            break
        path.append(sub)
        current = sub
    return path


def reorder_group_args(group: click.Group, ctx: click.Context, args: Sequence[str]) -> list[str]:
    """把当前组的选项与子命令选项按归属重排，不要求调用方按固定顺序传参。"""
    if not args:
        return []

    option_index = _collect_option_index(group, ctx)
    pieces = _tokenize(args, option_index)
    command_names = set(group.list_commands(ctx))
    self_names = _self_option_names(group)

    positionals = [piece.tokens[0] for piece in pieces if piece.kind == "pos"]
    first_command: str | None = None
    path_positionals: list[str] = []
    for name in positionals:
        if first_command is None:
            if name in command_names:
                first_command = name
                path_positionals.append(name)
            continue
        path_positionals.append(name)

    path = _resolve_path(group, ctx, path_positionals) if first_command is not None else []
    child_names: set[str] = set()
    if path:
        child_names = _subtree_option_names(path[0], ctx)

    self_tokens: list[str] = []
    remainder: list[str] = []
    pending_child_opts: list[str] = []
    seen_command = False

    for piece in pieces:
        if piece.kind == "end":
            remainder.extend(piece.tokens)
            continue
        if piece.kind == "pos":
            remainder.extend(piece.tokens)
            if not seen_command and piece.tokens[0] in command_names:
                seen_command = True
                remainder.extend(pending_child_opts)
                pending_child_opts = []
            continue

        name = piece.option_name or ""
        if name in _HELP_OPTION_NAMES:
            remainder.extend(piece.tokens)
            continue

        is_self = name in self_names
        on_child = name in child_names
        if is_self and (not on_child or not seen_command):
            self_tokens.extend(piece.tokens)
            continue

        if not seen_command:
            pending_child_opts.extend(piece.tokens)
        else:
            remainder.extend(piece.tokens)

    if pending_child_opts:
        remainder.extend(pending_child_opts)
    return self_tokens + remainder


class FlexibleGroup(click.Group):
    """组级选项与子命令选项均可交错书写的 Click Group。"""

    # Click 约定：group_class = type 表示嵌套 @group.group() 继续使用本类
    group_class = type

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        return super().parse_args(ctx, reorder_group_args(self, ctx, args))
