# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from shutil import copyfile
import re

from aws_doc_sdk_examples_tools import validator_config

from .file_utils import get_files, clear
from .metadata import Example
from .metadata_errors import MetadataErrors, MetadataError

SNIPPET_START = "snippet-start:["
SNIPPET_END = "snippet-end:["


@dataclass
class Snippet:
    id: str
    file: str
    line_start: int
    line_end: int
    code: str


@dataclass
class SnippetError(MetadataError):
    line: Optional[int] = None
    tag: Optional[str] = None

    def prefix(self):
        return super().prefix() + f" at l{self.line} for {self.tag}: "


@dataclass
class DuplicateSnippetStartError(SnippetError):
    def message(self):
        return "duplicate snippet-start tag"


@dataclass
class DuplicateSnippetEndError(SnippetError):
    def message(self):
        return "duplicate snippet-end tag"


@dataclass
class MissingSnippetStartError(SnippetError):
    def message(self):
        return "snippet-end with no matching start"


@dataclass
class MissingSnippetEndError(SnippetError):
    def message(self):
        return "snippet-start with no matching end"


@dataclass
class SnippetAlreadyWritten(MetadataError):
    def message(self):
        return "Snippet file already exists, which means this tag is defined more than once in separate files."


@dataclass
class SnippetWriteError(MetadataError):
    error: Any = None

    def message(self):
        return "Error writing snippet file."


@dataclass
class MetadataUnicodeError(MetadataError):
    err: Optional[UnicodeDecodeError] = None

    def message(self):
        return f" unicode error: {str(self.err)}"


def _tag_from_line(token: str, line: str, prefix: str) -> str:
    tag_start = line.find(token) + len(token)
    tag_end = line.find("]", tag_start)
    return prefix + line[tag_start:tag_end].strip()


def parse_snippets(
    lines: List[str], file: Path, prefix: str
) -> Tuple[Dict[str, Snippet], MetadataErrors]:
    snippets: Dict[str, Snippet] = {}
    errors = MetadataErrors()
    open_tags: Set[str] = set()
    for line_idx, line in enumerate(lines):
        if SNIPPET_START in line:
            tag = _tag_from_line(SNIPPET_START, line, prefix)
            if tag in snippets:
                errors.append(
                    DuplicateSnippetStartError(file=str(file), line=line_idx, tag=tag)
                )
            else:
                snippets[tag] = Snippet(
                    id=tag,
                    file=str(file),
                    line_start=line_idx,
                    line_end=-1,
                    code="",
                )
                open_tags.add(tag)
        elif SNIPPET_END in line:
            tag = _tag_from_line(SNIPPET_END, line, prefix)
            if tag not in snippets:
                errors.append(
                    MissingSnippetStartError(file=str(file), line=line_idx, tag=tag)
                )
            elif tag not in open_tags:
                errors.append(
                    DuplicateSnippetEndError(file=str(file), line=line_idx, tag=tag)
                )
            else:
                open_tags.remove(tag)
                snippets[tag].line_end = line_idx
        else:
            for tag in open_tags:
                snippets[tag].code += line

    for tag in open_tags:
        errors.append(
            MissingSnippetEndError(
                file=str(file), line=snippets[tag].line_start, tag=tag
            )
        )
    return snippets, errors


def find_snippets(file: Path, prefix: str) -> Tuple[Dict[str, Snippet], MetadataErrors]:
    errors = MetadataErrors()
    snippets: Dict[str, Snippet] = {}
    with open(file, encoding="utf-8") as snippet_file:
        try:
            snippets, errs = parse_snippets(snippet_file.readlines(), file, prefix)
            errors.extend(errs)
        except UnicodeDecodeError as err:
            errors.append(MetadataUnicodeError(file=str(file), err=err))
    return snippets, errors


def collect_snippets(
    root: Path, prefix: str = ""
) -> Tuple[Dict[str, Snippet], MetadataErrors]:
    snippets: Dict[str, Snippet] = {}
    errors = MetadataErrors()
    for file in get_files(root, validator_config.skip):
        snips, errs = find_snippets(file, prefix)
        snippets.update(snips)
        errors.extend(errs)
    return snippets, errors


@dataclass
class MissingSnippet(MetadataError):
    tag: Optional[str] = None

    def message(self):
        return f"missing snippet {self.tag}"


@dataclass
class MissingSnippetFile(MetadataError):
    snippet_file: Optional[str] = None

    def message(self):
        return f"missing snippet_file {self.snippet_file}"


@dataclass
class WindowsUnsafeSnippetFile(MetadataError):
    snippet_file: Optional[str] = None

    def message(self):
        return f"snippet_file with unsafe Windows name {self.snippet_file}"


# This set is from https://superuser.com/a/358861, but does not include / or \ as those are verified as the entire path
win_unsafe_re = r'[:*?"<>|]'


def validate_snippets(
    examples: List[Example],
    snippets: Dict[str, Snippet],
    snippet_files: Set[str],
    errors: MetadataErrors,
    root: Path,
):
    for example in examples:
        for lang in example.languages:
            language = example.languages[lang]
            for version in language.versions:
                for excerpt in version.excerpts:
                    for snippet_tag in excerpt.snippet_tags:
                        if snippet_tag not in snippets:
                            # Ensure all metadata snippets are found
                            errors.append(
                                MissingSnippet(
                                    file=example.file,
                                    id=f"{lang}:{version.sdk_version}",
                                    tag=snippet_tag,
                                )
                            )
                    for snippet_file in excerpt.snippet_files:
                        if not (root / snippet_file).exists():
                            # Ensure all snippet_files exist
                            errors.append(
                                MissingSnippetFile(
                                    file=example.file,
                                    snippet_file=snippet_file,
                                    id=f"{lang}:{version.sdk_version}",
                                )
                            )
                        if re.search(win_unsafe_re, str(snippet_file)):
                            errors.append(
                                WindowsUnsafeSnippetFile(
                                    file=example.file,
                                    snippet_file=snippet_file,
                                    id=f"{lang}:{version.sdk_version}",
                                )
                            )
                        snippet_files.add(snippet_file)


def write_snippets(root: Path, snippets: Dict[str, Snippet]):
    errors = MetadataErrors()
    for tag in snippets:
        name = root / f"{tag}.txt"
        if name.exists():
            errors.append(SnippetAlreadyWritten(file=str(name)))
        else:
            try:
                with open(name, "w", encoding="utf-8") as file:
                    file.write(snippets[tag].code)
            except Exception as error:
                errors.append(SnippetWriteError(file=str(name), error=error))
    return errors


def write_snippet_file(folder: Path, snippet_file: Path):
    name = str(snippet_file).replace("/", ".")
    dest = folder / f"{name}.txt"
    if not dest.exists():
        copyfile(folder / snippet_file, dest)


def main():
    root = Path(__file__).parent.parent.parent
    snippets, errors = collect_snippets(root)
    print(f"Found {len(snippets)} snippets")
    out = root / ".snippets"
    clear(out)
    errors.maybe_extend(write_snippets(out, snippets))
    if len(errors) > 0:
        print(errors)


if __name__ == "__main__":
    main()
