#!/usr/bin/env node

/**
 * Schulportal Hessen CLI - Version 1.0.0
 * 
 * A beautiful Terminal User Interface for the Schulportal Hessen API
 * Built with React (Ink) and TypeScript
 * 
 * Usage:
 *   npx lanis
 * 
 * Documentation:
 *   - TUI_QUICKSTART.md      - Get started in 30 seconds
 *   - tui/README.md          - Complete TUI documentation
 *   - tui/QUICK_REFERENCE.md - Keyboard shortcuts & workflows
 *   - tui/ADVANCED.md        - Development guide
 *   - tui/DESIGN_SYSTEM.md   - Design & customization
 */

import React from 'react';
import { render, Box, Text } from 'ink';
import App from './App.js';

// ASCII art header
const Header = () => (
  <Box flexDirection="column" marginBottom={1}>
    <Box marginBottom={0.5}>
      <Text bold color="cyan">
        ╔═══════════════════════════════════════════╗
      </Text>
    </Box>
    <Box marginBottom={0.5}>
      <Text bold color="cyan">
        ║   🎓 SCHULPORTAL HESSEN - Terminal CLI   ║
      </Text>
    </Box>
    <Box marginBottom={0.5}>
      <Text bold color="cyan">
        ║        v1.0.0 - Unofficial API           ║
      </Text>
    </Box>
    <Box marginBottom={0.5}>
      <Text bold color="cyan">
        ╚═══════════════════════════════════════════╝
      </Text>
    </Box>
  </Box>
);

const TUI = () => (
  <Box flexDirection="column">
    <Header />
    <App />
  </Box>
);

render(React.createElement(TUI));
