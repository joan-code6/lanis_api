import React, { useEffect } from 'react';
import { Box, Text } from 'ink';
import { useInput } from 'ink';
import SelectInput from 'ink-select-input';
import { Message } from '../types';

interface MessagesViewProps {
  messages: Message[];
  isLoading: boolean;
  error?: string;
  onSelectMessage: (message: Message) => void;
  onBack: () => void;
}

export const MessagesView: React.FC<MessagesViewProps> = ({
  messages,
  isLoading,
  error,
  onSelectMessage,
  onBack,
}) => {
  useInput((input, key) => {
    if (key.escape) {
      onBack();
    }
  });

  if (isLoading) {
    return (
      <Box flexDirection="column" padding={1}>
        <Text color="yellow">⟳ Nachrichten werden geladen...</Text>
      </Box>
    );
  }

  if (error) {
    return (
      <Box flexDirection="column" padding={1}>
        <Text color="red">✘ Fehler: {error}</Text>
        <Box marginTop={1}>
          <Text dimColor>Drücke Esc zum Zurück</Text>
        </Box>
      </Box>
    );
  }

  if (!Array.isArray(messages)) {
    return (
      <Box flexDirection="column" padding={1}>
        <Text color="red">✘ Fehler: Ungültiges Nachrichtenformat</Text>
        <Box marginTop={1}>
          <Text dimColor>Drücke Esc zum Zurück</Text>
        </Box>
      </Box>
    );
  }

  const messageItems = messages.map((msg, idx) => ({
    label: `${msg.sender.substring(0, 20).padEnd(20)} • ${msg.subject.substring(0, 30).padEnd(30)}`,
    value: idx,
  }));

  messageItems.push({ label: '← Zurück zum Menü', value: -1 });

  const handleSelect = (item: { value: number }) => {
    if (item.value === -1) {
      onBack();
    } else {
      onSelectMessage(messages[item.value]);
    }
  };

  return (
    <Box flexDirection="column" padding={1} borderStyle="round" borderColor="blue">
      <Box marginBottom={1}>
        <Text bold color="blue">
          📨 NACHRICHTEN
        </Text>
      </Box>

      <Box marginBottom={1}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      <Box marginBottom={1}>
        <SelectInput items={messageItems} onSelect={handleSelect} />
      </Box>

      <Box marginTop={1}>
        <Text dimColor>{messages.length} Nachrichten • ↑↓ zum Navigieren</Text>
      </Box>
    </Box>
  );
};
