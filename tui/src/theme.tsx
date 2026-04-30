import { Text } from 'ink';
import React from 'react';

export const colors = {
  primary: 'cyan',
  success: 'green',
  warning: 'yellow',
  error: 'red',
  info: 'blue',
  accent: 'magenta',
} as const;

export const icons = {
  check: '✓',
  cross: '✘',
  arrow: '→',
  bullet: '•',
  spinner: '⟳',
  info: 'ⓘ',
  warning: '⚠',
  error: '✘',
} as const;

export const Border = ({ char = '━' }: { char?: string } = {}) => char.repeat(44);

export const Divider = () => <Text>{Border({ char: '━' })}</Text>;

export const StatusBadge: React.FC<{ status: 'success' | 'error' | 'pending'; label: string }> = ({
  status,
  label,
}) => {
  const statusColors = {
    success: 'green' as const,
    error: 'red' as const,
    pending: 'yellow' as const,
  };

  const statusIcons = {
    success: '✓',
    error: '✘',
    pending: '⟳',
  };

  return (
    <Text color={statusColors[status]}>
      {statusIcons[status]} {label}
    </Text>
  );
};
