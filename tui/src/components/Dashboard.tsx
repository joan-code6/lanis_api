import React, { useState } from 'react';
import { Box, Text } from 'ink';
import SelectInput from 'ink-select-input';
import { SessionState } from '../types';

interface DashboardProps {
  session: SessionState;
  unreadMessages: number;
  upcomingEvents: number;
  onSelectModule: (module: string) => void;
  onLogout: () => void;
}

export const Dashboard: React.FC<DashboardProps> = ({
  session,
  unreadMessages,
  upcomingEvents,
  onSelectModule,
  onLogout,
}) => {
  const menuItems = [
    { label: `📨 Nachrichten ${unreadMessages > 0 ? `(${unreadMessages})` : ''}`, value: 'messages' },
    { label: `📅 Kalender ${upcomingEvents > 0 ? `(${upcomingEvents})` : ''}`, value: 'calendar' },
    { label: '🎓 Mein Unterricht', value: 'courses' },
    { label: '👤 Profil', value: 'profile' },
    { label: '⚙️  Einstellungen', value: 'settings' },
    { label: '🚪 Abmelden', value: 'logout' },
  ];

  const handleSelect = (item: { value: string }) => {
    if (item.value === 'logout') {
      onLogout();
    } else {
      onSelectModule(item.value);
    }
  };

  return (
    <Box flexDirection="column" padding={1}>
      <Box marginBottom={1} borderStyle="round" borderColor="green" padding={1}>
        <Box flexDirection="column">
          <Text bold color="green">
            ✦ WILLKOMMEN
          </Text>
          <Text>
            {session.username} • Schule: {session.schoolId}
          </Text>
        </Box>
      </Box>

      <Box marginBottom={2}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      <Box marginBottom={1}>
        <Text dimColor>Wähle eine Option:</Text>
      </Box>

      <Box marginBottom={2}>
        <SelectInput items={menuItems} onSelect={handleSelect} />
      </Box>

      <Box marginTop={1}>
        <Text dimColor>Esc zum Beenden • ↑↓ zum Navigieren</Text>
      </Box>
    </Box>
  );
};
