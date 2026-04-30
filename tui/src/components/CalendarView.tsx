import React from 'react';
import { Box, Text } from 'ink';
import { useInput } from 'ink';
import SelectInput from 'ink-select-input';
import { CalendarEvent } from '../types';

interface CalendarViewProps {
  events: CalendarEvent[];
  isLoading: boolean;
  error?: string;
  onBack: () => void;
}

export const CalendarView: React.FC<CalendarViewProps> = ({
  events,
  isLoading,
  error,
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
        <Text color="yellow">⟳ Kalender wird geladen...</Text>
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

  const eventItems = events.map((event) => ({
    label: `${event.date}${event.time ? ` ${event.time}` : ''} • ${event.title}`,
    value: event.id,
  }));

  eventItems.push({ label: '← Zurück zum Menü', value: 'back' });

  const handleSelect = (item: { value: string }) => {
    if (item.value === 'back') {
      onBack();
    }
  };

  return (
    <Box flexDirection="column" padding={1} borderStyle="round" borderColor="magenta">
      <Box marginBottom={1}>
        <Text bold color="magenta">
          📅 KALENDER
        </Text>
      </Box>

      <Box marginBottom={1}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      {events.length === 0 ? (
        <Box marginBottom={1}>
          <Text dimColor>Keine kommenden Termine</Text>
        </Box>
      ) : (
        <Box marginBottom={1} flexDirection="column">
          <SelectInput items={eventItems} onSelect={handleSelect} />
        </Box>
      )}

      <Box marginTop={1}>
        <Text dimColor>{events.length} Termine • ↑↓ zum Navigieren</Text>
      </Box>
    </Box>
  );
};
