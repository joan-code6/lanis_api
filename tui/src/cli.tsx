#!/usr/bin/env node

import React from 'react';
import { render, Box, Text } from 'ink';
import App from './App.js';

const StyledApp = () => (
  <Box flexDirection="column">
    <Box marginBottom={1}>
      <Text>
        <Text bold color="cyan">
          ╔══════════════════════════════════════╗
        </Text>
      </Text>
    </Box>
    <Box marginBottom={1}>
      <Text>
        <Text bold color="cyan">
          ║  SCHULPORTAL HESSEN - Terminal Client
        </Text>
      </Text>
    </Box>
    <Box marginBottom={1}>
      <Text>
        <Text bold color="cyan">
          ╚══════════════════════════════════════╝
        </Text>
      </Text>
    </Box>
    <App />
  </Box>
);

render(React.createElement(StyledApp));
