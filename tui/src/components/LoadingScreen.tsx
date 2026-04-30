import React from 'react';
import { Box, Text } from 'ink';
import Spinner from 'ink-spinner';

interface LoadingScreenProps {
  message?: string;
  error?: string;
}

export const LoadingScreen: React.FC<LoadingScreenProps> = ({ message = 'Wird angemeldet...', error }) => {
  if (error) {
    return (
      <Box flexDirection="column" padding={1} borderStyle="round" borderColor="red">
        <Text color="red">✘ {error}</Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column" padding={1} borderStyle="round" borderColor="cyan">
      <Box marginBottom={1}>
        <Spinner type="dots" />
        <Box marginLeft={1}>
          <Text color="cyan">{message}</Text>
        </Box>
      </Box>
    </Box>
  );
};
