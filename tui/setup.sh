#!/bin/bash

# Setup script for Schulportal Hessen API + TUI

echo "╔════════════════════════════════════════════╗"
echo "║  Schulportal Hessen API + TUI Setup        ║"
echo "╚════════════════════════════════════════════╝"
echo ""

# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    echo "✘ Node.js is not installed. Please install it first."
    echo "   Download from: https://nodejs.org/"
    exit 1
fi

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "✘ Python 3 is not installed. Please install it first."
    exit 1
fi

echo "✓ Node.js version: $(node --version)"
echo "✓ Python version: $(python3 --version)"
echo ""

# Setup TUI
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Setting up TUI (Terminal User Interface)..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ -f "package.json" ]; then
    echo "Installing Node.js dependencies..."
    npm install
    echo "✓ Node.js dependencies installed"
    
    echo ""
    echo "Building TypeScript..."
    npm run build
    echo "✓ TypeScript built"
else
    echo "✘ package.json not found"
    exit 1
fi

echo ""
echo "╔════════════════════════════════════════════╗"
echo "║  Setup Complete!                          ║"
echo "╚════════════════════════════════════════════╝"
echo ""
echo "To get started:"
echo ""
echo "1. From the project root, start the API server:"
echo "   python -m uvicorn api:app --reload"
echo ""
echo "2. In another terminal, run the TUI from this directory:"
echo "   npm run dev"
echo ""
