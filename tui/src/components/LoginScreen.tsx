import React, { useState, useEffect } from 'react';
import { Box, Text } from 'ink';
import TextInput from 'ink-text-input';
import { useInput } from 'ink';
import { LoginCredentials } from '../types';
import { storage } from '../storage';

interface LoginScreenProps {
  onSubmit: (credentials: LoginCredentials) => void;
  isLoading: boolean;
  error?: string;
}

export const LoginScreen: React.FC<LoginScreenProps> = ({ onSubmit, isLoading, error }) => {
  const [step, setStep] = useState<'school' | 'username' | 'password'>('school');
  const [schoolId, setSchoolId] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  useEffect(() => {
    // Load saved credentials if they exist
    const saved = storage.getCredentials();
    if (saved) {
      setSchoolId(saved.schoolId);
      setUsername(saved.username);
      setPassword(saved.password);
    }
  }, []);

  const handleSubmit = () => {
    if (step === 'school') {
      setStep('username');
    } else if (step === 'username') {
      setStep('password');
    } else if (step === 'password') {
      onSubmit({ schoolId, username, password });
    }
  };

  return (
    <Box flexDirection="column" padding={1} borderStyle="round" borderColor="cyan">
      <Box marginBottom={1}>
        <Text bold color="cyan">
          ✦ SCHULPORTAL HESSEN
        </Text>
      </Box>

      <Box marginBottom={1}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      {step === 'school' && (
        <Box flexDirection="column" marginBottom={1}>
          <Text>Schul-ID:</Text>
          <TextInput
            value={schoolId}
            onChange={setSchoolId}
            onSubmit={handleSubmit}
            placeholder="z.B. 1234"
          />
        </Box>
      )}

      {step === 'username' && (
        <Box flexDirection="column" marginBottom={1}>
          <Text>Schul-ID: {schoolId}</Text>
          <Box marginBottom={0.5}>
            <Text dimColor>Benutzername:</Text>
          </Box>
          <TextInput
            value={username}
            onChange={setUsername}
            onSubmit={handleSubmit}
            placeholder="vorname.nachname"
          />
        </Box>
      )}

      {step === 'password' && (
        <Box flexDirection="column" marginBottom={1}>
          <Text>Schul-ID: {schoolId}</Text>
          <Text>Benutzername: {username}</Text>
          <Box marginBottom={0.5}>
            <Text dimColor>Passwort:</Text>
          </Box>
          <TextInput
            value={password}
            onChange={setPassword}
            onSubmit={handleSubmit}
            mask="•"
          />
        </Box>
      )}

      {error && (
        <Box marginBottom={1}>
          <Text color="red">✘ {error}</Text>
        </Box>
      )}

      {isLoading && (
        <Box marginTop={1}>
          <Text color="yellow">⟳ Wird authentifiziert...</Text>
        </Box>
      )}

      <Box marginTop={1}>
        <Text dimColor>{step === 'password' ? 'Enter drücken zum Anmelden' : 'Enter drücken zum Fortfahren'}</Text>
      </Box>
    </Box>
  );
};
