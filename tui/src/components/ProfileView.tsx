import React from 'react';
import { Box, Text } from 'ink';
import { useInput } from 'ink';
import SelectInput from 'ink-select-input';
import { SessionState } from '../types';

interface ProfileViewProps {
  session: SessionState;
  profile?: any;
  isLoading: boolean;
  error?: string;
  onBack: () => void;
}

export const ProfileView: React.FC<ProfileViewProps> = ({
  session,
  profile,
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
        <Text color="yellow">⟳ Profil wird geladen...</Text>
      </Box>
    );
  }

  const menuItems = [{ label: '← Zurück zum Menü', value: 'back' }];

  const handleSelect = (item: { value: string }) => {
    if (item.value === 'back') {
      onBack();
    }
  };

  return (
    <Box flexDirection="column" padding={1} borderStyle="round" borderColor="cyan">
      <Box marginBottom={1}>
        <Text bold color="cyan">
          👤 PROFIL
        </Text>
      </Box>

      <Box marginBottom={1}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      <Box flexDirection="column" marginBottom={1}>
        <Box marginBottom={0.5}>
          <Text>
            <Text bold>Benutzername:</Text> {session.username}
          </Text>
        </Box>
        <Box marginBottom={0.5}>
          <Text>
            <Text bold>Schul-ID:</Text> {session.schoolId}
          </Text>
        </Box>
        <Box marginBottom={0.5}>
          <Text>
            <Text bold>Verschlüsselung:</Text>{' '}
            <Text color={session.encryptionReady ? 'green' : 'red'}>
              {session.encryptionReady ? '✓ Bereit' : '✗ Nicht bereit'}
            </Text>
          </Text>
        </Box>

        {error && (
          <Box marginTop={0.5}>
            <Text color="red">✘ {error}</Text>
          </Box>
        )}
      </Box>

      <Box marginBottom={1}>
        <SelectInput items={menuItems} onSelect={handleSelect} />
      </Box>

      <Box marginTop={1}>
        <Text dimColor>Enter zum Auswählen</Text>
      </Box>
    </Box>
  );
};
