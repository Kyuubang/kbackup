# kbackup

[![PyPI](https://img.shields.io/pypi/v/kbackup.svg)](https://pypi.org/project/kbackup/)
[![Changelog](https://img.shields.io/github/v/release/Kyuubang/kbackup?include_prereleases&label=changelog)](https://github.com/Kyuubang/kbackup/releases)
[![Tests](https://github.com/Kyuubang/kbackup/actions/workflows/test.yml/badge.svg)](https://github.com/Kyuubang/kbackup/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/Kyuubang/kbackup/blob/master/LICENSE)

kubernetes manifest backup with well-structured folder

## Installation

Install this tool using `pip`:
```bash
pip install kbackup
```
## Usage

For help, run:
```bash
kbackup --help
```
You can also use:
```bash
python -m kbackup --help
```
## Development

To contribute to this tool, first checkout the code. Then create a new virtual environment:
```bash
cd kbackup
python -m venv venv
source venv/bin/activate
```
Now install the dependencies and test dependencies:
```bash
pip install -e '.[test]'
```
To run the tests:
```bash
python -m pytest
```
