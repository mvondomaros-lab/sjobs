# sjobs

[![made-with-python](https://img.shields.io/badge/Made%20with-Python-green.svg)](https://www.python.org/)
[![MIT license](https://img.shields.io/badge/License-MIT-green.svg)](https://lbesson.mit-license.org/)

Show Slurm jobs. Public, but not intended for general use.

## First-time Setup

- Setup the the [uv](https://docs.astral.sh/uv/) package manager:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```
  
- Add the following lines to your `uv.toml` (usually located under `~/.config/uv/uv.toml`):
    ```toml
    [[index]]
    name = "mvondomaros-lab"
    url = "https://mvondomaros-lab.github.io"
    ```

## Usage

- Run `sjobs` without installation:
    ```bash
    uvx sjobs  
    ```  

- Install `sjobs` permanently:
    ```bash
    uv tool install sjobs
    ```

