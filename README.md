# Deduce LSP

This is a (currently experimental) language server protocol for the [Deduce](https://github.com/jsiek/deduce/) programming language

Created using this extension template: https://github.com/microsoft/vscode-python-tools-extension-template


[pygls]: https://github.com/openlawlibrary/pygls

## Features

- Syntax Checking
- Token autocomplete
- Jump to definition
- Definition on hover
- Signature Advice 


More to come

## Known issues
- Go-to for operators works a bit strangely on compound operators. For example, `<=` may send to `<`, depending on where is clicked.
  - Potential fix : smarter regex for word at position, instead of using bespoke function

## Installation

This is currently an extension in pre-release on the [marketplace](https://marketplace.visualstudio.com/manage/publishers/calvinjosenhans/extensions/deduce-lsp/hub?_a=acquisition)


## Requirements
- TODO

## Release Notes

### 0.0.4
- Deduce version sync
- Go-to definition functionality for operators

### 0.0.3
- Small speedups
- Induction autofill

### 0.0.2
- Some polish and bug fixing in parsing

### 0.0.1
- Bare minimum