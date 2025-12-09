# Lanis API CLI

A command-line interface for Schulportal Hessen (Lanis), built with Typer and Rich.

## Installation

1. Install the requirements:
   ```bash
   pip install -r ../requirements.txt
   ```

## Usage

### Interactive Mode (Recommended)
Simply run the script to enter the interactive menu:
```bash
python main.py
```
You can navigate the menus using arrow keys and select options with Enter.

### Login
If you are not logged in, the interactive mode will prompt you.
You can also login directly:
```bash
python main.py login
```

### Legacy Commands
The old command-line arguments are still supported but the interactive mode is preferred.

## Notes
- The CLI uses the functions from the `functions` directory.
- Credentials are saved in plain text in `sph_config.json` (for now). Be careful.
