# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Implementation of tool support over LSP."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import sys
import sysconfig
import traceback
from typing import Any, Optional, Sequence

from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse


# **********************************************************
# Update sys.path before importing any bundled libraries.
# **********************************************************
def update_sys_path(path_to_add: str, strategy: str) -> None:
    """Add given path to `sys.path`."""
    if path_to_add not in sys.path and os.path.isdir(path_to_add):
        if strategy == "useBundled":
            sys.path.insert(0, path_to_add)
        elif strategy == "fromEnvironment":
            sys.path.append(path_to_add)


# Ensure that we can import LSP libraries, and other bundled libraries.
update_sys_path(
    os.fspath(pathlib.Path(__file__).parent.parent / "libs"),
    os.getenv("LS_IMPORT_STRATEGY", "useBundled"),
)

# **********************************************************
# Imports needed for the language server goes below this.
# **********************************************************
# pylint: disable=wrong-import-position,import-error
import lsp_jsonrpc as jsonrpc
import lsp_utils as utils
import lsprotocol.types as lsp
from pygls import server, uris, workspace
from pygls.workspace import TextDocument

WORKSPACE_SETTINGS = {}
GLOBAL_SETTINGS = {}
RUNNER = pathlib.Path(__file__).parent / "lsp_runner.py"

MAX_WORKERS = 5


from rec_desc_parser import init_parser, parse, parse_statement, end_of_file, set_deduce_directory
import rec_desc_parser as parser
from error import ParseError
from abstract_syntax import *
import proof_checker

import asyncio



set_deduce_directory(".")
init_parser()



def find_tok_diff(new, old):
    for i , (n, o) in enumerate(zip(new, old)):
        if n != o: return i
    
    if len(new) > len(old): 
        # Addition to the end
        return len(old)
    elif len(old)> len(new): 
        # Deletion from the end
        return len(new)
    else: 
        return -1
    

class DeduceItem():
    """Items in the index"""

    def __init__(self, loc : Meta, ty, str, comp_ty : lsp.CompletionItemKind, ast_node):
        self.loc = loc
        self.ty = ty
        self.str = str
        self.completion = comp_ty
        self.ast = ast_node


class DocIndex():
    def __init__(self):
        self.stmts = []
        self.stmt_is = []
        self.data = {}
        self.one_grams = {}
        # self.tokens = {}
    

    def add(self, k : str, v):
        for c in k:
            self.one_grams.setdefault(c, set()).add(k)
        
        self.data[k] = v

    def search(self, k : str):
        s : set = self.one_grams.get(k[0], set())
        for c in k[1:]:
            s = s.intersection(self.one_grams.get(c, set()))
        
        return list(filter(lambda w : w.find(k) != -1, s))


    def __contains__(self, item):
        return item in self.data
    
    def __getitem__(self, item):
        return self.data[item]

# Nodes where I will sync parsing
stmt_like = set([Assert, Define, RecFun, Theorem, Import, Print, Union])

class DeduceLanguageServer(server.LanguageServer):
    """Language Server for Deduce"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.index = {}
        self.diagnostics = {}
        self.pending_tasks = {}

    async def debounce_parse(self, doc : TextDocument):
        """Debounce function to delay parsing after rapid edits."""

        PARSE_DELAY = 0.5  # Adjust as needed
        
        if doc.uri in self.pending_tasks:
            self.pending_tasks[doc.uri].cancel()  # Cancel previous task
            try:
                await self.pending_tasks[doc.uri]  # Ensure proper cancellation handling
            except asyncio.CancelledError:
                pass  # Ignore cancellation exceptions
            
    
        async def delayed_parse():
            try:
                await asyncio.sleep(PARSE_DELAY)  # Wait for more edits
                self.inc_parse(doc)  # Ensure parsing completes before proceeding
            except asyncio.CancelledError:
                pass  # Silently ignore cancelled tasks
    
        task = asyncio.create_task(delayed_parse())
        self.pending_tasks[doc.uri] = task
        await task

    # def tok_at_position(self, uri : str, position):
    #     if uri not in self.index:
    #         return None
        
    #     p_hash = position.line + "," + position.character

    #     if p_hash not in self.index[uri].tokens:
    #         return None
    
    #     return self.index[uri].tokens[p_hash]

    def inc_parse(self, doc : TextDocument):
        # doc_index = {"stmts": [], "stmt_is": []} if doc.uri not in self.index else self.index[doc.uri]
        doc_index = self.index.get(doc.uri, DocIndex())

        stmts = doc_index.stmts
        stmt_is = doc_index.stmt_is

        lexed = list(parser.lark_parser.lex(doc.source))

        change_i = find_tok_diff(lexed, parser.token_list)
    
        if change_i < 0: 
            return
    
        # Index of the first changed statement
        stmt_i = next((i-1 for i, x in enumerate(stmt_is) if x > change_i), 0)
    
        parser.token_list = lexed
        parser.current_position = stmt_is[stmt_i] if stmt_is != [] else 0
    
        stmts = stmts[:stmt_i]
        stmt_is = stmt_is[:stmt_i]

        try:
            imports = []
            while not end_of_file():
                stmt = parse_statement()
                
                stmt_is.append(parser.current_position)
                stmts.append(stmt)
    
                match stmt:
                    case Define(meta, name, ty, body, priv):
                        doc_index.add(name, DeduceItem(meta, ty, str(stmt), lsp.CompletionItemKind.Variable, stmt))
                    case RecFun(meta, name, type_params, param_types, return_type, cases, priv):
                        # TODO: I'm being lazy wrt types
                        doc_index.add(name, DeduceItem(meta, None, stmt.pretty_print(0), lsp.CompletionItemKind.Function, stmt))
                    case Theorem(meta, name, what, proof, priv):
                        # Theorems don't have a type
                        doc_index.add(name, DeduceItem(meta, None, str(what), lsp.CompletionItemKind.Function, stmt))
                    case Union(meta, name, typarams, constr_list, priv):
                        pretty = name + "{\n\t" \
                        + "\n\t".join([str(c) for c in constr_list]) + "\n}"

                        doc_index.add(name, DeduceItem(meta, None, pretty, lsp.CompletionItemKind.Struct, stmt))
                        for c in constr_list:
                            doc_index.add(c.name, DeduceItem(c.location, None, pretty, lsp.CompletionItemKind.Variable, stmt))
                    case Import(meta, name):
                        # TODO: Be smarter about what could be included
                        base_path = os.path.dirname(doc.path)

                        potential_path = os.path.join(base_path, name + ".pf")

                        if os.path.exists(potential_path):
                            imports.append(self.workspace.get_text_document(Path(potential_path).absolute().as_uri()))
                        else:
                            potential_path = os.path.join(base_path, 'lib', name + ".pf")
                            if os.path.exists(potential_path):
                                imports.append(self.workspace.get_text_document(Path(potential_path).absolute().as_uri()))
                    case _: # Irrelevant statements
                        pass
            
            for i in imports:
                self.inc_parse(i)
            
        except ParseError as e:
            self.diagnostics[doc.uri]= [lsp.Diagnostic(
                    message=e.base_message(),
                    severity=lsp.DiagnosticSeverity.Error,
                    range=lsp.Range(
                        start=lsp.Position(line=e.loc.line-1, character=e.loc.column-1),
                        end=lsp.Position(line=e.loc.end_line-1, character=e.loc.end_column-1)
                    )
                )]
        else:
            self.diagnostics[doc.uri] = []
                
        self.index[doc.uri] = doc_index

        return stmts




LSP_SERVER = DeduceLanguageServer(
    name="Deduce Language Server", version="0.0.3", max_workers=MAX_WORKERS
)

@LSP_SERVER.feature(
        lsp.TEXT_DOCUMENT_SIGNATURE_HELP,
        lsp.SignatureHelpOptions(trigger_characters=["(", ")", "[", "]", "<", ">"])
)
def signature_help(ls : DeduceLanguageServer, params: lsp.SignatureHelpParams):
    doc = ls.workspace.get_text_document(params.text_document.uri)
    current_line = doc.lines[params.position.line].strip()
    
    fun_match = False
    fun_i = 0
    fun_name = ""


    brack_loc = None

    # TODO: Better parsing for where the characters are, esp since mixing functions and theorems
    # Also because the theorem could have multiple blocks
    for m in re.finditer(r"[([<](([^)],?)*)", current_line):
        if m.start() <= params.position.character and m.end() >=  params.position.character:
            fun_match = m
            fun_i = params.position.character - m.start()
            brack_loc = m.start()
            break

    if brack_loc == None: 
        brack_loc = params.position.character
    else:

        rev = { '<': '>', '[': ']' }
    
        i = brack_loc
        while i > 0 and (current_line[brack_loc - 1]) == ">" or (current_line[brack_loc - 1]) == "]":
            i -= 1
            if rev[current_line[i]] == current_line[brack_loc-1]:
                brack_loc = i
        


    fun_name = doc.word_at_position(
        lsp.Position(
            params.position.line,
            brack_loc - 1
        )
    )


    fun_sig = fun_name

    for uri in ls.index:
        if fun_name in ls.index[uri]:
            fun_sig = ls.index[uri][fun_name].str.split("\n")[0][:-1]

    # TODO: Use fun_i to do bold in the markdown help?
    # TODO: Combine both types of complete
    if fun_match:
        active_param = len(fun_match.group(1)[:fun_i].split(",")) - 1

        return lsp.SignatureHelp(
            [
                lsp.SignatureInformation(
                label=fun_sig,
                # documentation="Look for docstring?",
                # parameters=[
                #     lsp.ParameterInformation("asdf", "A thing"),
                #     lsp.ParameterInformation("qwer", "Parameter 2"),
                # ],
                # active_parameter=active_param
            )]
        )

@LSP_SERVER.feature(
    lsp.TEXT_DOCUMENT_COMPLETION,
)
def completions(ls : DeduceLanguageServer, params: lsp.CompletionParams):
    doc = ls.workspace.get_document(params.text_document.uri)
    current_line = doc.lines[params.position.line].strip()

    word = doc.word_at_position(params.position)

    res = []

    for uri in ls.index:
        ops = ls.index[uri].search(word)

        for k in ops:
            # TODO: Induction advice, need these things from index
            if isinstance(ls.index[uri][k].ast, Union) and current_line.startswith("induction"):
                match ls.index[uri][k].ast:
                  case Union(loc2, name, typarams, alts, isPrivate):
                    ind_advice = 'induction ' + k + '\n'

                    for alt in alts:
                        match alt:
                          case Constructor(loc3, constr_name, param_types):
                            ind_params = [proof_checker.type_first_letter(ty)+str(i+1)\
                              for i,ty in enumerate(param_types)]
                            ind_advice += 'case ' + base_name(constr_name)
                            if len(param_types) > 0:
                              ind_advice += '(' + ', '.join(ind_params) + ')'
                            num_recursive = sum([1 if proof_checker.is_recursive(name, ty) else 0 \
                                                 for ty in param_types])
                            if num_recursive > 0:
                              rec_params =[(p,ty) for (p,ty) in zip(ind_params,param_types)\
                                           if proof_checker.is_recursive(name, ty)]
                              ind_advice += ' assume '
                              ind_advice += ', '.join(['IH' + str(i+1) \
                                    for i, (param,param_ty) in enumerate(rec_params)])

                            ind_advice += ' {\n\t\t  ?\n}\n'


                    res.append(
                        lsp.CompletionItem(
                            label = "induction " + k,
                            # insert_text=ind_advice,
                            text_edit= lsp.TextEdit(
                                lsp.Range(
                                    start=lsp.Position(params.position.line, params.position.character - len(current_line)),
                                    end=lsp.Position(params.position.line, params.position.character)
                                ),
                                ind_advice
                            )
                    ))

            res.append(lsp.CompletionItem(label=k, 
                                   kind=ls.index[uri][k].completion))

    return res


# Hover
@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_HOVER)
def hover(ls : DeduceLanguageServer, params: lsp.HoverParams):
    pos = params.position
    document_uri = params.text_document.uri
    document = ls.workspace.get_text_document(document_uri)

    try:
        line : str = document.lines[pos.line]
    except IndexError:
        return None

    # Get the word hovered
    word = document.word_at_position(params.position)

    if word == '':
        # TODO: Look for operators
        pass

    if word == '': return

    word_i = 0
    
    for m in re.finditer(word, line):
        if m.start() <= params.position.character and m.end() >=  params.position.character:
            word_i = m.start()


    for k in ls.index:
        if word in ls.index[k]:
            return lsp.Hover(
                contents=lsp.MarkupContent(
                    kind=lsp.MarkupKind.PlainText,
                    value=ls.index[k][word].str
                ),
                range=lsp.Range(
                    start=lsp.Position(line=pos.line, character=word_i),
                    end=lsp.Position(line=pos.line, character=word_i + len(word))
                )
            )


all_ops = parser.expt_operators.union(parser.mult_operators).union(parser.add_operators).union(parser.compare_operators).union(parser.equal_operators).union(parser.iff_operators) 
max_op_len = 3 # Just hardcoding for now

def look_for_op(l, pos):
    pos = pos-max_op_len + 1
    for i in range(max_op_len, 0, -1):
        if l[pos:pos+i] in all_ops:
            return l[pos:pos+i]


# TODO: Scope
# Once uniquify is working, then use that output every change or whatever!
@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def goto_definition(ls: DeduceLanguageServer, params: lsp.DefinitionParams):
    """Jump to an object's definition."""
    doc = ls.workspace.get_text_document(params.text_document.uri)

    word = doc.word_at_position(params.position)

    if word == 'operator' or word == '':
        l = doc.lines[params.position.line]
        i = params.position.character

        while i < len(l):
            o = look_for_op(l, i)
            if o:
                word = o
                break
            i += 1

    # Prioritize the currently open document
    if doc.uri in ls.index and word in ls.index[doc.uri]:
        loc : Meta = ls.index[doc.uri][word].loc

        return lsp.Location(uri=doc.uri, range=lsp.Range(
            start=lsp.Position(line=loc.line-1, character=loc.column - 1),
            end=lsp.Position(line=loc.line-1, character=loc.column - 1)
        ))
    else:
        for uri in ls.index:
            if word in ls.index[uri]:
                loc : Meta = ls.index[uri][word].loc

                return lsp.Location(uri=uri, range=lsp.Range(
                    start=lsp.Position(line=loc.line-1, character=loc.column - 1),
                    end=lsp.Position(line=loc.line-1, character=loc.column - 1)
                ))



@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
async def did_open(ls : DeduceLanguageServer, params: lsp.DidOpenTextDocumentParams):
    """Parse each document when it is opened"""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    await ls.debounce_parse(doc)

    diagnostics = ls.diagnostics[doc.uri]

    LSP_SERVER.publish_diagnostics(
        uri=params.text_document.uri,
        diagnostics = diagnostics)


@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
async def did_save(ls : DeduceLanguageServer, params: lsp.DidChangeTextDocumentParams):
    """Parse each document when it is changed"""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    await ls.debounce_parse(doc)

    diagnostics = ls.diagnostics[doc.uri]

    LSP_SERVER.publish_diagnostics(
        uri=params.text_document.uri,
        diagnostics = diagnostics)


@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
async def did_save(ls : DeduceLanguageServer, params: lsp.DidSaveTextDocumentParams):
    """Parse each document when it is saved"""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    await ls.debounce_parse(doc)

    diagnostics = ls.diagnostics[doc.uri]

    LSP_SERVER.publish_diagnostics(
        uri=params.text_document.uri,
        diagnostics = diagnostics)


# **********************************************************
# Tool specific code goes below this.
# **********************************************************

# Reference:
#  LS Protocol:
#  https://microsoft.github.io/language-server-protocol/specifications/specification-3-16/
#
#  Sample implementations:
#  Pylint: https://github.com/microsoft/vscode-pylint/blob/main/bundled/tool
#  Black: https://github.com/microsoft/vscode-black-formatter/blob/main/bundled/tool
#  isort: https://github.com/microsoft/vscode-isort/blob/main/bundled/tool

TOOL_MODULE = "deduce-lsp"

TOOL_DISPLAY = "Deduce Language Server"

# TODO: Update TOOL_ARGS with default argument you have to pass to your tool in
# all scenarios.
TOOL_ARGS = []  # default arguments always passed to your tool.


# TODO: If your tool is a linter then update this section.
# Delete "Linting features" section if your tool is NOT a linter.
# **********************************************************
# Linting features start here
# **********************************************************

#  See `pylint` implementation for a full featured linter extension:
#  Pylint: https://github.com/microsoft/vscode-pylint/blob/main/bundled/tool


# @LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
# def did_open(params: lsp.DidOpenTextDocumentParams) -> None:
#     """LSP handler for textDocument/didOpen request."""
#     document = LSP_SERVER.workspace.get_document(params.text_document.uri)
#     diagnostics: list[lsp.Diagnostic] = _linting_helper(document)
#     LSP_SERVER.publish_diagnostics(document.uri, diagnostics)


# @LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
# def did_save(params: lsp.DidSaveTextDocumentParams) -> None:
#     """LSP handler for textDocument/didSave request."""
#     document = LSP_SERVER.workspace.get_document(params.text_document.uri)
#     diagnostics: list[lsp.Diagnostic] = _linting_helper(document)
#     LSP_SERVER.publish_diagnostics(document.uri, diagnostics)


# @LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
# def did_close(params: lsp.DidCloseTextDocumentParams) -> None:
#     """LSP handler for textDocument/didClose request."""
#     document = LSP_SERVER.workspace.get_document(params.text_document.uri)
#     # Publishing empty diagnostics to clear the entries for this file.
#     LSP_SERVER.publish_diagnostics(document.uri, [])


def _linting_helper(document: workspace.Document) -> list[lsp.Diagnostic]:
    # TODO: Determine if your tool supports passing file content via stdin.
    # If you want to support linting on change then your tool will need to
    # support linting over stdin to be effective. Read, and update
    # _run_tool_on_document and _run_tool functions as needed for your project.
    result = _run_tool_on_document(document)
    return _parse_output_using_regex(result.stdout) if result.stdout else []


# TODO: If your linter outputs in a known format like JSON, then parse
# accordingly. But incase you need to parse the output using RegEx here
# is a helper you can work with.
# flake8 example:
# If you use following format argument with flake8 you can use the regex below to parse it.
# TOOL_ARGS += ["--format='%(row)d,%(col)d,%(code).1s,%(code)s:%(text)s'"]
# DIAGNOSTIC_RE =
#    r"(?P<line>\d+),(?P<column>-?\d+),(?P<type>\w+),(?P<code>\w+\d+):(?P<message>[^\r\n]*)"
DIAGNOSTIC_RE = re.compile(r"")


def _parse_output_using_regex(content: str) -> list[lsp.Diagnostic]:
    lines: list[str] = content.splitlines()
    diagnostics: list[lsp.Diagnostic] = []

    # TODO: Determine if your linter reports line numbers starting at 1 (True) or 0 (False).
    line_at_1 = True
    # TODO: Determine if your linter reports column numbers starting at 1 (True) or 0 (False).
    column_at_1 = True

    line_offset = 1 if line_at_1 else 0
    col_offset = 1 if column_at_1 else 0
    for line in lines:
        if line.startswith("'") and line.endswith("'"):
            line = line[1:-1]
        match = DIAGNOSTIC_RE.match(line)
        if match:
            data = match.groupdict()
            position = lsp.Position(
                line=max([int(data["line"]) - line_offset, 0]),
                character=int(data["column"]) - col_offset,
            )
            diagnostic = lsp.Diagnostic(
                range=lsp.Range(
                    start=position,
                    end=position,
                ),
                message=data.get("message"),
                severity=_get_severity(data["code"], data["type"]),
                code=data["code"],
                source=TOOL_MODULE,
            )
            diagnostics.append(diagnostic)

    return diagnostics


# TODO: if you want to handle setting specific severity for your linter
# in a user configurable way, then look at look at how it is implemented
# for `pylint` extension from our team.
# Pylint: https://github.com/microsoft/vscode-pylint
# Follow the flow of severity from the settings in package.json to the server.
def _get_severity(*_codes: list[str]) -> lsp.DiagnosticSeverity:
    # TODO: All reported issues from linter are treated as warning.
    # change it as appropriate for your linter.
    return lsp.DiagnosticSeverity.Warning


# **********************************************************
# Linting features end here
# **********************************************************

# TODO: If your tool is a formatter then update this section.
# Delete "Formatting features" section if your tool is NOT a
# formatter.
# **********************************************************
# Formatting features start here
# **********************************************************
#  Sample implementations:
#  Black: https://github.com/microsoft/vscode-black-formatter/blob/main/bundled/tool


# @LSP_SERVER.feature(lsp.TEXT_DOCUMENT_FORMATTING)
# def formatting(params: lsp.DocumentFormattingParams) -> list[lsp.TextEdit] | None:
#     """LSP handler for textDocument/formatting request."""
#     # If your tool is a formatter you can use this handler to provide
#     # formatting support on save. You have to return an array of lsp.TextEdit
#     # objects, to provide your formatted results.

#     document = LSP_SERVER.workspace.get_document(params.text_document.uri)
#     edits = _formatting_helper(document)
#     if edits:
#         return edits

#     # NOTE: If you provide [] array, VS Code will clear the file of all contents.
#     # To indicate no changes to file return None.
#     return None


def _formatting_helper(document: workspace.Document) -> list[lsp.TextEdit] | None:
    # TODO: For formatting on save support the formatter you use must support
    # formatting via stdin.
    # Read, and update_run_tool_on_document and _run_tool functions as needed
    # for your formatter.
    result = _run_tool_on_document(document, use_stdin=True)
    if result.stdout:
        new_source = _match_line_endings(document, result.stdout)
        return [
            lsp.TextEdit(
                range=lsp.Range(
                    start=lsp.Position(line=0, character=0),
                    end=lsp.Position(line=len(document.lines), character=0),
                ),
                new_text=new_source,
            )
        ]
    return None


def _get_line_endings(lines: list[str]) -> str:
    """Returns line endings used in the text."""
    try:
        if lines[0][-2:] == "\r\n":
            return "\r\n"
        return "\n"
    except Exception:  # pylint: disable=broad-except
        return None


def _match_line_endings(document: workspace.Document, text: str) -> str:
    """Ensures that the edited text line endings matches the document line endings."""
    expected = _get_line_endings(document.source.splitlines(keepends=True))
    actual = _get_line_endings(text.splitlines(keepends=True))
    if actual == expected or actual is None or expected is None:
        return text
    return text.replace(actual, expected)


# **********************************************************
# Formatting features ends here
# **********************************************************


# **********************************************************
# Required Language Server Initialization and Exit handlers.
# **********************************************************
@LSP_SERVER.feature(lsp.INITIALIZE)
def initialize(params: lsp.InitializeParams) -> None:
    """LSP handler for initialize request."""
    log_to_output(f"CWD Server: {os.getcwd()}")

    paths = "\r\n   ".join(sys.path)
    log_to_output(f"sys.path used to run Server:\r\n   {paths}")

    GLOBAL_SETTINGS.update(**params.initialization_options.get("globalSettings", {}))

    settings = params.initialization_options["settings"]
    _update_workspace_settings(settings)
    log_to_output(
        f"Settings used to run Server:\r\n{json.dumps(settings, indent=4, ensure_ascii=False)}\r\n"
    )
    log_to_output(
        f"Global settings:\r\n{json.dumps(GLOBAL_SETTINGS, indent=4, ensure_ascii=False)}\r\n"
    )


@LSP_SERVER.feature(lsp.EXIT)
def on_exit(_params: Optional[Any] = None) -> None:
    """Handle clean up on exit."""
    jsonrpc.shutdown_json_rpc()


@LSP_SERVER.feature(lsp.SHUTDOWN)
def on_shutdown(_params: Optional[Any] = None) -> None:
    """Handle clean up on shutdown."""
    jsonrpc.shutdown_json_rpc()


def _get_global_defaults():
    return {
        "path": GLOBAL_SETTINGS.get("path", []),
        "interpreter": GLOBAL_SETTINGS.get("interpreter", [sys.executable]),
        "args": GLOBAL_SETTINGS.get("args", []),
        "importStrategy": GLOBAL_SETTINGS.get("importStrategy", "useBundled"),
        "showNotifications": GLOBAL_SETTINGS.get("showNotifications", "off"),
    }


def _update_workspace_settings(settings):
    if not settings:
        key = os.getcwd()
        WORKSPACE_SETTINGS[key] = {
            "cwd": key,
            "workspaceFS": key,
            "workspace": uris.from_fs_path(key),
            **_get_global_defaults(),
        }
        return

    for setting in settings:
        key = uris.to_fs_path(setting["workspace"])
        WORKSPACE_SETTINGS[key] = {
            "cwd": key,
            **setting,
            "workspaceFS": key,
        }


def _get_settings_by_path(file_path: pathlib.Path):
    workspaces = {s["workspaceFS"] for s in WORKSPACE_SETTINGS.values()}

    while file_path != file_path.parent:
        str_file_path = str(file_path)
        if str_file_path in workspaces:
            return WORKSPACE_SETTINGS[str_file_path]
        file_path = file_path.parent

    setting_values = list(WORKSPACE_SETTINGS.values())
    return setting_values[0]


def _get_document_key(document: workspace.Document):
    if WORKSPACE_SETTINGS:
        document_workspace = pathlib.Path(document.path)
        workspaces = {s["workspaceFS"] for s in WORKSPACE_SETTINGS.values()}

        # Find workspace settings for the given file.
        while document_workspace != document_workspace.parent:
            if str(document_workspace) in workspaces:
                return str(document_workspace)
            document_workspace = document_workspace.parent

    return None


def _get_settings_by_document(document: workspace.Document | None):
    if document is None or document.path is None:
        return list(WORKSPACE_SETTINGS.values())[0]

    key = _get_document_key(document)
    if key is None:
        # This is either a non-workspace file or there is no workspace.
        key = os.fspath(pathlib.Path(document.path).parent)
        return {
            "cwd": key,
            "workspaceFS": key,
            "workspace": uris.from_fs_path(key),
            **_get_global_defaults(),
        }

    return WORKSPACE_SETTINGS[str(key)]


# *****************************************************
# Internal execution APIs.
# *****************************************************
def _run_tool_on_document(
    document: workspace.Document,
    use_stdin: bool = False,
    extra_args: Optional[Sequence[str]] = None,
) -> utils.RunResult | None:
    """Runs tool on the given document.

    if use_stdin is true then contents of the document is passed to the
    tool via stdin.
    """
    if extra_args is None:
        extra_args = []
    if str(document.uri).startswith("vscode-notebook-cell"):
        # TODO: Decide on if you want to skip notebook cells.
        # Skip notebook cells
        return None

    if utils.is_stdlib_file(document.path):
        # TODO: Decide on if you want to skip standard library files.
        # Skip standard library python files.
        return None

    # deep copy here to prevent accidentally updating global settings.
    settings = copy.deepcopy(_get_settings_by_document(document))

    code_workspace = settings["workspaceFS"]
    cwd = settings["cwd"]

    use_path = False
    use_rpc = False
    if settings["path"]:
        # 'path' setting takes priority over everything.
        use_path = True
        argv = settings["path"]
    elif settings["interpreter"] and not utils.is_current_interpreter(
        settings["interpreter"][0]
    ):
        # If there is a different interpreter set use JSON-RPC to the subprocess
        # running under that interpreter.
        argv = [TOOL_MODULE]
        use_rpc = True
    else:
        # if the interpreter is same as the interpreter running this
        # process then run as module.
        argv = [TOOL_MODULE]

    argv += TOOL_ARGS + settings["args"] + extra_args

    if use_stdin:
        # TODO: update these to pass the appropriate arguments to provide document contents
        # to tool via stdin.
        # For example, for pylint args for stdin looks like this:
        #     pylint --from-stdin <path>
        # Here `--from-stdin` path is used by pylint to make decisions on the file contents
        # that are being processed. Like, applying exclusion rules.
        # It should look like this when you pass it:
        #     argv += ["--from-stdin", document.path]
        # Read up on how your tool handles contents via stdin. If stdin is not supported use
        # set use_stdin to False, or provide path, what ever is appropriate for your tool.
        argv += []
    else:
        argv += [document.path]

    if use_path:
        # This mode is used when running executables.
        log_to_output(" ".join(argv))
        log_to_output(f"CWD Server: {cwd}")
        result = utils.run_path(
            argv=argv,
            use_stdin=use_stdin,
            cwd=cwd,
            source=document.source.replace("\r\n", "\n"),
        )
        if result.stderr:
            log_to_output(result.stderr)
    elif use_rpc:
        # This mode is used if the interpreter running this server is different from
        # the interpreter used for running this server.
        log_to_output(" ".join(settings["interpreter"] + ["-m"] + argv))
        log_to_output(f"CWD Linter: {cwd}")

        result = jsonrpc.run_over_json_rpc(
            workspace=code_workspace,
            interpreter=settings["interpreter"],
            module=TOOL_MODULE,
            argv=argv,
            use_stdin=use_stdin,
            cwd=cwd,
            source=document.source,
        )
        if result.exception:
            log_error(result.exception)
            result = utils.RunResult(result.stdout, result.stderr)
        elif result.stderr:
            log_to_output(result.stderr)
    else:
        # In this mode the tool is run as a module in the same process as the language server.
        log_to_output(" ".join([sys.executable, "-m"] + argv))
        log_to_output(f"CWD Linter: {cwd}")
        # This is needed to preserve sys.path, in cases where the tool modifies
        # sys.path and that might not work for this scenario next time around.
        with utils.substitute_attr(sys, "path", sys.path[:]):
            try:
                # TODO: `utils.run_module` is equivalent to running `python -m <pytool-module>`.
                # If your tool supports a programmatic API then replace the function below
                # with code for your tool. You can also use `utils.run_api` helper, which
                # handles changing working directories, managing io streams, etc.
                # Also update `_run_tool` function and `utils.run_module` in `lsp_runner.py`.
                result = utils.run_module(
                    module=TOOL_MODULE,
                    argv=argv,
                    use_stdin=use_stdin,
                    cwd=cwd,
                    source=document.source,
                )
            except Exception:
                log_error(traceback.format_exc(chain=True))
                raise
        if result.stderr:
            log_to_output(result.stderr)

    log_to_output(f"{document.uri} :\r\n{result.stdout}")
    return result


def _run_tool(extra_args: Sequence[str]) -> utils.RunResult:
    """Runs tool."""
    # deep copy here to prevent accidentally updating global settings.
    settings = copy.deepcopy(_get_settings_by_document(None))

    code_workspace = settings["workspaceFS"]
    cwd = settings["workspaceFS"]

    use_path = False
    use_rpc = False
    if len(settings["path"]) > 0:
        # 'path' setting takes priority over everything.
        use_path = True
        argv = settings["path"]
    elif len(settings["interpreter"]) > 0 and not utils.is_current_interpreter(
        settings["interpreter"][0]
    ):
        # If there is a different interpreter set use JSON-RPC to the subprocess
        # running under that interpreter.
        argv = [TOOL_MODULE]
        use_rpc = True
    else:
        # if the interpreter is same as the interpreter running this
        # process then run as module.
        argv = [TOOL_MODULE]

    argv += extra_args

    if use_path:
        # This mode is used when running executables.
        log_to_output(" ".join(argv))
        log_to_output(f"CWD Server: {cwd}")
        result = utils.run_path(argv=argv, use_stdin=True, cwd=cwd)
        if result.stderr:
            log_to_output(result.stderr)
    elif use_rpc:
        # This mode is used if the interpreter running this server is different from
        # the interpreter used for running this server.
        log_to_output(" ".join(settings["interpreter"] + ["-m"] + argv))
        log_to_output(f"CWD Linter: {cwd}")
        result = jsonrpc.run_over_json_rpc(
            workspace=code_workspace,
            interpreter=settings["interpreter"],
            module=TOOL_MODULE,
            argv=argv,
            use_stdin=True,
            cwd=cwd,
        )
        if result.exception:
            log_error(result.exception)
            result = utils.RunResult(result.stdout, result.stderr)
        elif result.stderr:
            log_to_output(result.stderr)
    else:
        # In this mode the tool is run as a module in the same process as the language server.
        log_to_output(" ".join([sys.executable, "-m"] + argv))
        log_to_output(f"CWD Linter: {cwd}")
        # This is needed to preserve sys.path, in cases where the tool modifies
        # sys.path and that might not work for this scenario next time around.
        with utils.substitute_attr(sys, "path", sys.path[:]):
            try:
                # TODO: `utils.run_module` is equivalent to running `python -m <pytool-module>`.
                # If your tool supports a programmatic API then replace the function below
                # with code for your tool. You can also use `utils.run_api` helper, which
                # handles changing working directories, managing io streams, etc.
                # Also update `_run_tool_on_document` function and `utils.run_module` in `lsp_runner.py`.
                result = utils.run_module(
                    module=TOOL_MODULE, argv=argv, use_stdin=True, cwd=cwd
                )
            except Exception:
                log_error(traceback.format_exc(chain=True))
                raise
        if result.stderr:
            log_to_output(result.stderr)

    log_to_output(f"\r\n{result.stdout}\r\n")
    return result


# *****************************************************
# Logging and notification.
# *****************************************************
def log_to_output(
    message: str, msg_type: lsp.MessageType = lsp.MessageType.Log
) -> None:
    LSP_SERVER.show_message_log(message, msg_type)


def log_error(message: str) -> None:
    LSP_SERVER.show_message_log(message, lsp.MessageType.Error)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["onError", "onWarning", "always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Error)


def log_warning(message: str) -> None:
    LSP_SERVER.show_message_log(message, lsp.MessageType.Warning)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["onWarning", "always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Warning)


def log_always(message: str) -> None:
    LSP_SERVER.show_message_log(message, lsp.MessageType.Info)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Info)


# *****************************************************
# Start the server.
# *****************************************************
if __name__ == "__main__":
    LSP_SERVER.start_io()
