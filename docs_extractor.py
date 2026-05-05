"""
Docstring extractor for sph_client - generates markdown documentation from code.
Run this to extract docs that can be loaded into AI context.
"""

import ast
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_class_methods(filepath: Path) -> Dict[str, Any]:
    """Extract class methods and their docstrings from a file."""
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()
    
    tree = ast.parse(source)
    methods = {}
    
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            docstring = ast.get_docstring(node)
            if docstring:
                methods[node.name] = {
                    "docstring": docstring,
                    "args": [arg.arg for arg in node.args.args],
                    "defaults": node.args.defaults,
                }
    
    return methods


def extract_api_docs(project_root: Path) -> str:
    """Extract all API docs and format as markdown."""
    
    modules = {
        "base": project_root / "schulportal_hessen" / "base.py",
        "login": project_root / "schulportal_hessen" / "applets" / "login" / "api.py",
        "kalender": project_root / "schulportal_hessen" / "applets" / "kalender" / "api.py",
        "dsb": project_root / "schulportal_hessen" / "external" / "dsb" / "api.py",
    }
    
    docs = ["## sph_client API Documentation\n"]
    
    for name, path in modules.items():
        if path.exists():
            methods = get_class_methods(path)
            docs.append(f"\n### {name.upper()}\n")
            for method_name, info in methods.items():
                if info["docstring"]:
                    docs.append(f"#### {method_name}\n")
                    docs.append(f"{info['docstring']}\n")
    
    return "\n".join(docs)


def save_docs(project_root: Path, output_path: Optional[Path] = None):
    """Generate and save documentation."""
    if output_path is None:
        output_path = project_root / "docs" / "API.md"
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    docs = extract_api_docs(project_root)
    output_path.write_text(docs, encoding="utf-8")
    print(f"Documentation saved to {output_path}")


if __name__ == "__main__":
    project_root = Path(__file__).parent
    save_docs(project_root)