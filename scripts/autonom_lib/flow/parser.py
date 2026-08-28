"""Flow v1 YAML-subset parser: text → positioned node tree.

Deliberately not a YAML parser. The accepted grammar is sized to exactly what
the Flow v1 schema needs — block mappings, block sequences, single-line flow
sequences (``[a, b]``), plain/single/double-quoted scalars, comments, and one
``---`` separator between header and commands — and everything else YAML
would tolerate is refused with a positioned error: tabs in indentation,
anchors, aliases, tags, directives, block scalars, flow mappings, merge keys,
duplicate keys, multi-line plain scalars.

One bounded exception exists for Maestro import (``allow_flow_mappings``):
single-line flow mappings ``{key: value, ...}`` with scalar values — the most
common idiom in real Maestro files (``tapOn: {text: X}``). The native Flow v1
grammar never accepts them; only the Maestro importer turns the mode on.

Every rejection raises ``AutonomError(FLOW_PARSE_ERROR)`` whose message is
prefixed ``<file>:<line>:<column>:`` and whose extras carry ``file``,
``line``, ``column``, and a machine-stable ``reason`` slug — a flow author
(human or agent) gets an exact location, an agent gets a branchable detail.

This module knows nothing about the flow schema: no command names, no header
fields. Typing (bool/int coercion) happens in ``schema.py`` against the node
positions kept here, which is what kills the Norway problem — a quoted
``"true"`` in a bool slot is a positioned type error, never a silent string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import errors

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$", re.S)
_INTERP_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class Scalar:
    text: str
    style: str  # plain | single | double
    line: int
    col: int


@dataclass
class Sequence:
    items: list = field(default_factory=list)
    line: int = 0
    col: int = 0


@dataclass
class Mapping:
    pairs: list = field(default_factory=list)  # list[tuple[Scalar, node]]
    line: int = 0
    col: int = 0

    def get(self, name: str):
        for key, value in self.pairs:
            if key.text == name:
                return value
        return None

    def keys(self) -> list[str]:
        return [key.text for key, _ in self.pairs]


@dataclass
class FlowDocument:
    header: Mapping
    commands: Sequence
    separator_line: int
    path: str


@dataclass
class _Line:
    number: int
    indent: int
    text: str


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


class _Parser:
    def __init__(self, raw_lines: list[str], base_line: int, path: str,
                 allow_flow_mappings: bool = False) -> None:
        self.path = path
        self.allow_flow_mappings = allow_flow_mappings
        self.lines: list[_Line] = []
        for offset, raw in enumerate(raw_lines):
            number = base_line + offset
            line = raw.rstrip("\r")
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            if "\t" in line[:indent] or stripped.startswith("\t"):
                self._err("tab_indent", "tab in indentation; use spaces",
                          number, line.index("\t") + 1,
                          hint="Flow files are indented with spaces only.")
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("%") and indent == 0:
                self._err("directive", "YAML directives are not part of Flow v1",
                          number, 1)
            self.lines.append(_Line(number, indent, stripped))
        self.pos = 0

    # -- plumbing -------------------------------------------------------------

    def _err(self, reason: str, message: str, line: int, col: int,
             hint: str | None = None) -> None:
        raise errors.AutonomError(
            errors.FLOW_PARSE_ERROR,
            f"{self.path}:{line}:{col}: {message}",
            hint=hint, file=self.path, line=line, column=col, reason=reason,
        )

    def _cur(self) -> _Line | None:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def _advance(self) -> None:
        self.pos += 1

    # -- entry points ---------------------------------------------------------

    def parse_top_mapping(self, empty_line: int) -> Mapping:
        first = self._cur()
        if first is None:
            return Mapping([], empty_line, 1)
        if first.indent != 0:
            self._err("bad_indent", "top-level content must start at column 1",
                      first.number, first.indent + 1)
        if _is_item(first.text):
            self._err("header_not_mapping",
                      "the flow header must be a mapping, not a sequence",
                      first.number, 1)
        node = self._parse_mapping(0)
        self._require_consumed()
        return node

    def parse_top_sequence(self, empty_line: int) -> Sequence:
        first = self._cur()
        if first is None:
            return Sequence([], empty_line, 1)
        if first.indent != 0:
            self._err("bad_indent", "top-level content must start at column 1",
                      first.number, first.indent + 1)
        if not _is_item(first.text):
            self._err("commands_not_sequence",
                      "flow commands must be a sequence of '- <command>' items",
                      first.number, 1)
        node = self._parse_sequence(0)
        self._require_consumed()
        return node

    def _require_consumed(self) -> None:
        leftover = self._cur()
        if leftover is not None:
            self._err("bad_dedent",
                      "indentation does not match any enclosing block",
                      leftover.number, leftover.indent + 1)

    # -- block parsing --------------------------------------------------------

    def _parse_mapping(self, indent: int,
                       first: tuple[int, int, str] | None = None) -> Mapping:
        pairs: list = []
        seen: dict[str, int] = {}
        start = first if first else None
        node_line = first[0] if first else self._cur().number  # type: ignore[union-attr]
        node_col = (first[1] if first else indent) + 1

        while True:
            if start is not None:
                lineno, col0, text = start
                start = None
            else:
                cur = self._cur()
                if cur is None or cur.indent != indent:
                    break
                if _is_item(cur.text):
                    self._err("item_in_mapping",
                              "sequence item at mapping indentation",
                              cur.number, cur.indent + 1,
                              hint="Indent '- ' items deeper than their key.")
                lineno, col0, text = cur.number, cur.indent, cur.text
                self._advance()

            key, inline = self._parse_pair(text, lineno, col0)
            if key.text in seen:
                self._err("duplicate_key",
                          f"duplicate key {key.text!r} (first at line {seen[key.text]})",
                          key.line, key.col)
            seen[key.text] = key.line

            nxt = self._cur()
            if inline is None:
                if nxt is None or nxt.indent <= indent:
                    if nxt is not None and nxt.indent == indent and _is_item(nxt.text):
                        self._err("sequence_not_indented",
                                  "block sequences must be indented deeper than their key",
                                  nxt.number, nxt.indent + 1,
                                  hint="Indent the '- ' items by two spaces, "
                                       "or use an inline list: key: [a, b].")
                    self._err("missing_value",
                              f"key {key.text!r} has no value",
                              key.line, key.col + len(key.text) + 1)
                value = self._parse_child_block(nxt.indent)
            else:
                if nxt is not None and nxt.indent > indent:
                    self._err("unexpected_indent",
                              "indented block under a key that already has an inline value",
                              nxt.number, nxt.indent + 1)
                value = inline
            pairs.append((key, value))

        return Mapping(pairs, node_line, node_col)

    def _parse_sequence(self, indent: int) -> Sequence:
        first_line = self._cur().number  # type: ignore[union-attr]
        items: list = []
        while True:
            cur = self._cur()
            if cur is None or cur.indent != indent:
                break
            if not _is_item(cur.text):
                self._err("key_in_sequence",
                          "mapping key at sequence-item indentation",
                          cur.number, cur.indent + 1)
            if cur.text == "-":
                self._err("empty_sequence_item", "empty sequence item",
                          cur.number, cur.indent + 1,
                          hint="Write the command on the same line: '- launchApp'.")
            content = cur.text[1:].lstrip(" ")
            content_col0 = cur.indent + (len(cur.text) - len(content))
            lineno = cur.number
            self._advance()
            if _KEY_RE.match(content) and self._pair_shaped(content):
                items.append(self._parse_mapping(content_col0,
                                                 first=(lineno, content_col0, content)))
            else:
                items.append(self._parse_inline_value(content, lineno, content_col0))
                nxt = self._cur()
                if nxt is not None and nxt.indent > indent:
                    self._err("unexpected_indent",
                              "indented block under a scalar sequence item",
                              nxt.number, nxt.indent + 1)
        return Sequence(items, first_line, indent + 1)

    @staticmethod
    def _pair_shaped(text: str) -> bool:
        match = _KEY_RE.match(text)
        if not match:
            return False
        rest = match.group(2)
        return rest == "" or rest.startswith(" ")

    def _parse_child_block(self, child_indent: int):
        cur = self._cur()
        if _is_item(cur.text):  # type: ignore[union-attr]
            return self._parse_sequence(child_indent)
        return self._parse_mapping(child_indent)

    # -- pair and value parsing ----------------------------------------------

    def _parse_pair(self, text: str, lineno: int, col0: int):
        match = _KEY_RE.match(text)
        if match is None or not self._pair_shaped(text):
            colon = re.match(r"^(\"[^\"]*\"|'[^']*'|\S+?):(\s|$)", text, re.S)
            if colon:
                self._err("invalid_key",
                          f"invalid mapping key {colon.group(1)!r}",
                          lineno, col0 + 1,
                          hint="Keys are plain identifiers: letters, digits, '_', '-'.")
            if match is not None:  # identifier key but no space after ':'
                self._err("missing_space_after_colon",
                          "':' must be followed by a space or end of line",
                          lineno, col0 + len(match.group(1)) + 2)
            self._err("expected_key", "expected 'key:' or 'key: value'",
                      lineno, col0 + 1)
        key_text = match.group(1)
        key = Scalar(key_text, "plain", lineno, col0 + 1)
        rest = match.group(2)
        if rest == "":
            return key, None
        value_text = rest.lstrip(" ")
        vcol0 = col0 + len(key_text) + 1 + (len(rest) - len(value_text))
        if value_text == "" or value_text.startswith("#"):
            return key, None
        return key, self._parse_inline_value(value_text, lineno, vcol0)

    def _parse_inline_value(self, text: str, lineno: int, col0: int):
        first = text[0]
        if first == "[":
            return self._parse_flow_sequence(text, lineno, col0)
        if first == "{":
            if self.allow_flow_mappings:
                return self._parse_flow_mapping(text, lineno, col0)
            self._err("flow_mapping", "flow mappings ({...}) are not part of Flow v1",
                      lineno, col0 + 1,
                      hint="Use a nested block mapping instead.")
        if first in "'\"":
            scalar, end = self._parse_quoted(text, lineno, col0)
            self._require_only_comment(text, end, lineno, col0)
            return scalar
        if first in "|>":
            self._err("block_scalar",
                      "block scalars (| and >) are not part of Flow v1",
                      lineno, col0 + 1,
                      hint="Flow v1 values are single-line.")
        if first == "&":
            self._err("anchor", "anchors are not part of Flow v1", lineno, col0 + 1)
        if first == "*":
            self._err("alias", "aliases are not part of Flow v1", lineno, col0 + 1)
        if first == "!":
            self._err("tag", "tags are not part of Flow v1", lineno, col0 + 1)
        if text == "-" or text.startswith("- "):
            self._err("sequence_on_value_line",
                      "a block sequence cannot start on the value line",
                      lineno, col0 + 1,
                      hint="Put the '- ' items on the following lines, indented.")
        if text.startswith("? "):
            self._err("explicit_key", "explicit keys are not part of Flow v1",
                      lineno, col0 + 1)
        # plain scalar: cut a trailing comment (a '#' preceded by whitespace)
        value = text
        for i in range(1, len(value)):
            if value[i] == "#" and value[i - 1] == " ":
                value = value[:i]
                break
        value = value.rstrip(" ")
        scalar = Scalar(value, "plain", lineno, col0 + 1)
        self._check_interpolation(scalar)
        return scalar

    def _parse_quoted(self, text: str, lineno: int, col0: int):
        quote = text[0]
        if quote == "'":
            i = 1
            out: list[str] = []
            while i < len(text):
                ch = text[i]
                if ch == "'":
                    if i + 1 < len(text) and text[i + 1] == "'":
                        out.append("'")
                        i += 2
                        continue
                    scalar = Scalar("".join(out), "single", lineno, col0 + 1)
                    self._check_interpolation(scalar)
                    return scalar, i + 1
                out.append(ch)
                i += 1
            self._err("unterminated_quote", "unterminated single-quoted scalar",
                      lineno, col0 + 1)
        i = 1
        out = []
        escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
        while i < len(text):
            ch = text[i]
            if ch == '"':
                scalar = Scalar("".join(out), "double", lineno, col0 + 1)
                self._check_interpolation(scalar)
                return scalar, i + 1
            if ch == "\\":
                if i + 1 >= len(text):
                    self._err("unterminated_quote",
                              "unterminated double-quoted scalar", lineno, col0 + 1)
                esc = text[i + 1]
                if esc in escapes:
                    out.append(escapes[esc])
                    i += 2
                    continue
                if esc == "u":
                    hexpart = text[i + 2:i + 6]
                    if len(hexpart) == 4 and all(c in "0123456789abcdefABCDEF" for c in hexpart):
                        out.append(chr(int(hexpart, 16)))
                        i += 6
                        continue
                    self._err("invalid_escape", "invalid \\u escape (need 4 hex digits)",
                              lineno, col0 + i + 1)
                self._err("invalid_escape", f"unknown escape \\{esc}",
                          lineno, col0 + i + 1,
                          hint="Supported: \\\\ \\\" \\n \\t \\r \\uXXXX.")
            out.append(ch)
            i += 1
        self._err("unterminated_quote", "unterminated double-quoted scalar",
                  lineno, col0 + 1)

    def _require_only_comment(self, text: str, end: int, lineno: int, col0: int) -> None:
        rest = text[end:].lstrip(" ")
        if rest and not rest.startswith("#"):
            junk_offset = end + (len(text[end:]) - len(rest))
            self._err("trailing_content",
                      "unexpected content after the closing quote",
                      lineno, col0 + junk_offset + 1)

    def _parse_flow_sequence(self, text: str, lineno: int, col0: int) -> Sequence:
        items: list = []
        i = 1
        current_start = i
        parts: list[tuple[int, str]] = []  # (offset0, raw item text)
        while i < len(text):
            ch = text[i]
            if ch in "'\"":
                # skip over a quoted region (with escapes for double quotes)
                quote = ch
                i += 1
                while i < len(text):
                    if quote == '"' and text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == quote:
                        if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                            i += 2
                            continue
                        break
                    i += 1
                if i >= len(text):
                    self._err("unterminated_quote", "unterminated quote in inline list",
                              lineno, col0 + current_start + 1)
                i += 1
                continue
            if ch == "[":
                self._err("nested_flow_sequence",
                          "nested inline lists are not part of Flow v1",
                          lineno, col0 + i + 1)
            if ch == ",":
                parts.append((current_start, text[current_start:i]))
                current_start = i + 1
                i += 1
                continue
            if ch == "]":
                parts.append((current_start, text[current_start:i]))
                self._require_only_comment(text, i + 1, lineno, col0)
                if len(parts) == 1 and parts[0][1].strip() == "":
                    return Sequence([], lineno, col0 + 1)  # empty []
                for offset0, raw in parts:
                    item = raw.strip(" ")
                    if item == "":
                        self._err("empty_flow_item", "empty item in inline list",
                                  lineno, col0 + offset0 + 1)
                    icol0 = col0 + offset0 + (len(raw) - len(raw.lstrip(" ")))
                    if item[0] in "'\"":
                        scalar, end = self._parse_quoted(item, lineno, icol0)
                        if item[end:].strip(" "):
                            self._err("trailing_content",
                                      "unexpected content after the closing quote",
                                      lineno, icol0 + end + 1)
                        items.append(scalar)
                    elif item[0] in "[{":
                        self._err("nested_flow_sequence",
                                  "nested inline collections are not part of Flow v1",
                                  lineno, icol0 + 1)
                    else:
                        scalar = Scalar(item, "plain", lineno, icol0 + 1)
                        self._check_interpolation(scalar)
                        items.append(scalar)
                return Sequence(items, lineno, col0 + 1)
            i += 1
        self._err("unterminated_flow_sequence", "inline list has no closing ']'",
                  lineno, col0 + 1)

    def _parse_flow_mapping(self, text: str, lineno: int, col0: int) -> Mapping:
        """Import-mode courtesy: a single-line ``{key: value, ...}``.

        Values are scalars only — nested collections, block continuations,
        and everything else stay refused. Reached only when
        ``allow_flow_mappings`` is on (the Maestro importer)."""
        pairs: list = []
        seen: dict[str, int] = {}
        i = 1
        current_start = i
        prev = "{"  # last significant (non-space) top-level char
        parts: list[tuple[int, str]] = []  # (offset0, raw entry text)
        while i < len(text):
            ch = text[i]
            # A quote opens a region only at a value/entry start (after '{',
            # ',' or ':'), matching YAML — a mid-word apostrophe (Don't) is
            # plain-scalar content, not a quote.
            if ch in "'\"" and prev in "{,:":
                quote = ch
                i += 1
                while i < len(text):
                    if quote == '"' and text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == quote:
                        if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                            i += 2
                            continue
                        break
                    i += 1
                if i >= len(text):
                    self._err("unterminated_quote",
                              "unterminated quote in flow mapping",
                              lineno, col0 + current_start + 1)
                i += 1
                prev = quote
                continue
            if ch in "[{":
                self._err("nested_flow_mapping",
                          "nested inline collections are not part of the "
                          "flow-mapping form",
                          lineno, col0 + i + 1,
                          hint="Use a nested block mapping instead.")
            if ch == ",":
                parts.append((current_start, text[current_start:i]))
                current_start = i + 1
                i += 1
                prev = ","
                continue
            if ch == "}":
                parts.append((current_start, text[current_start:i]))
                self._require_only_comment(text, i + 1, lineno, col0)
                if len(parts) == 1 and parts[0][1].strip() == "":
                    return Mapping([], lineno, col0 + 1)  # empty {}
                for offset0, raw in parts:
                    entry = raw.strip(" ")
                    if entry == "":
                        self._err("empty_flow_item",
                                  "empty entry in flow mapping",
                                  lineno, col0 + offset0 + 1)
                    ecol0 = col0 + offset0 + (len(raw) - len(raw.lstrip(" ")))
                    match = _KEY_RE.match(entry)
                    if match is None:
                        self._err("expected_key",
                                  "expected 'key: value' in flow mapping",
                                  lineno, ecol0 + 1,
                                  hint="Keys are plain identifiers: letters, "
                                       "digits, '_', '-'.")
                    key_text = match.group(1)
                    rest = match.group(2)
                    if rest != "" and not rest.startswith(" "):
                        self._err("missing_space_after_colon",
                                  "':' must be followed by a space",
                                  lineno, ecol0 + len(key_text) + 2)
                    if key_text in seen:
                        self._err("duplicate_key",
                                  f"duplicate key {key_text!r} in flow mapping",
                                  lineno, ecol0 + 1)
                    seen[key_text] = lineno
                    key = Scalar(key_text, "plain", lineno, ecol0 + 1)
                    value_text = rest.lstrip(" ")
                    if value_text == "":
                        self._err("missing_value",
                                  f"key {key_text!r} has no value",
                                  lineno, ecol0 + len(key_text) + 2)
                    vcol0 = ecol0 + len(key_text) + 1 + (len(rest) - len(value_text))
                    if value_text[0] in "'\"":
                        scalar, end = self._parse_quoted(value_text, lineno, vcol0)
                        if value_text[end:].strip(" "):
                            self._err("trailing_content",
                                      "unexpected content after the closing quote",
                                      lineno, vcol0 + end + 1)
                        pairs.append((key, scalar))
                    else:
                        scalar = Scalar(value_text.rstrip(" "), "plain",
                                        lineno, vcol0 + 1)
                        self._check_interpolation(scalar)
                        pairs.append((key, scalar))
                return Mapping(pairs, lineno, col0 + 1)
            if ch != " ":
                prev = ch
            i += 1
        self._err("unterminated_flow_mapping", "flow mapping has no closing '}'",
                  lineno, col0 + 1)

    def _check_interpolation(self, scalar: Scalar) -> None:
        text = scalar.text
        i = 0
        while True:
            j = text.find("${", i)
            if j < 0:
                return
            if j > 0 and text[j - 1] == "$":
                i = j + 2
                continue
            match = _INTERP_RE.match(text, j)
            if not match:
                self._err("invalid_interpolation",
                          "invalid ${...} interpolation (expected ${NAME})",
                          scalar.line, scalar.col + j,
                          hint="Variable names match [A-Za-z_][A-Za-z0-9_]*; "
                               "write $${ for a literal ${.")
            i = match.end()


def parse_document(text: str, path: str,
                   allow_flow_mappings: bool = False) -> FlowDocument:
    """Parse one flow file: header mapping, one ``---``, command sequence."""
    raw_lines = text.split("\n")
    separators = [i for i, line in enumerate(raw_lines) if line.strip() == "---"]
    scratch = _Parser([], 1, path)  # error helper for document-level problems
    if not separators:
        last_line = len(raw_lines)
        if raw_lines and raw_lines[-1] == "":
            last_line -= 1  # the trailing newline is not a line of content
        scratch._err("missing_separator",
                     "missing '---' separator between header and commands",
                     max(last_line, 1), 1,
                     hint="A flow file is: header fields, a '---' line, then commands.")
    if len(separators) > 1:
        scratch._err("multiple_separators", "more than one '---' separator",
                     separators[1] + 1, 1)
    sep = separators[0]
    header_parser = _Parser(raw_lines[:sep], 1, path,
                            allow_flow_mappings=allow_flow_mappings)
    header = header_parser.parse_top_mapping(empty_line=1)
    commands_parser = _Parser(raw_lines[sep + 1:], sep + 2, path,
                              allow_flow_mappings=allow_flow_mappings)
    commands = commands_parser.parse_top_sequence(empty_line=sep + 2)
    return FlowDocument(header, commands, sep + 1, path)
