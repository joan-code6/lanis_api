import React from 'react';
import { Box, Text } from 'ink';

interface CardProps {
  title?: string;
  children: React.ReactNode;
  color?: string;
  padding?: number;
}

export const Card: React.FC<CardProps> = ({ title, children, color = 'cyan', padding = 1 }) => {
  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={color}
      padding={padding}
      marginBottom={1}
    >
      {title && (
        <Box marginBottom={1}>
          <Text bold color={color}>
            {title}
          </Text>
        </Box>
      )}
      {children}
    </Box>
  );
};

interface ListItemProps {
  label: string;
  value?: string;
  color?: string;
}

export const ListItem: React.FC<ListItemProps> = ({ label, value, color }) => {
  return (
    <Box marginBottom={0.5}>
      <Text>
        <Text bold>• {label}:</Text>
        {value && <Text color={color}> {value}</Text>}
      </Text>
    </Box>
  );
};

interface StackProps {
  gap?: number;
  children: React.ReactNode;
}

export const VStack: React.FC<StackProps> = ({ gap = 1, children }) => {
  return (
    <Box flexDirection="column" marginBottom={gap}>
      {children}
    </Box>
  );
};

export const HStack: React.FC<StackProps> = ({ gap = 1, children }) => {
  return (
    <Box flexDirection="row" marginRight={gap}>
      {children}
    </Box>
  );
};
