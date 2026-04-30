import React, { useState, useEffect } from 'react';
import { Box, Text } from 'ink';
import { useInput } from 'ink';
import Spinner from 'ink-spinner';
import { api } from '../api';
import { Message } from '../types';

interface MessageDetailViewProps {
  message: Message;
  onBack: () => void;
}

export const MessageDetailView: React.FC<MessageDetailViewProps> = ({
  message,
  onBack,
}) => {
  const [content, setContent] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadContent = async () => {
      if (message.raw?.Uniquid) {
        try {
          const result = await api.getMessageContent(message.raw.Uniquid);
          if (result?.messages && result.messages.length > 0) {
            const msg = result.messages[0];
            let rawContent = msg.Inhalt || msg.content || msg.text || msg.Nachricht || '';
            
            // Clean up the content
            rawContent = rawContent
              .replace(/<br\s*\/?>/gi, '\n')
              .replace(/<\/p>/gi, '\n')
              .replace(/<[^>]*>/g, '')
              .replace(/&nbsp;/g, ' ')
              .replace(/&amp;/g, '&')
              .replace(/&lt;/g, '<')
              .replace(/&gt;/g, '>')
              .replace(/\n\s*\n/g, '\n')
              .trim();
            
            setContent(rawContent || 'Kein Inhalt verfügbar');
          } else {
            setContent('Keine Details verfügbar');
          }
        } catch (err) {
          setError('Fehler beim Laden der Nachricht');
        }
      } else {
        setContent('Keine ID verfügbar');
      }
      setIsLoading(false);
    };
    loadContent();
  }, [message]);

  useInput((input, key) => {
    if (key.escape) {
      onBack();
    }
  });

  return (
    <Box flexDirection="column" padding={1} borderStyle="round" borderColor="blue">
      <Box marginBottom={1}>
        <Text bold color="blue">
          📖 NACHRICHT
        </Text>
      </Box>

      <Box marginBottom={1}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      <Box flexDirection="column" marginBottom={1}>
        <Text bold>Von: <Text color="cyan">{message.sender}</Text></Text>
        <Text bold>Betreff: <Text color="cyan">{message.subject}</Text></Text>
        <Text dimColor>Datum: {message.timestamp}</Text>
      </Box>

      <Box marginBottom={1}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      {isLoading ? (
        <Box>
          <Spinner />
          <Text> Laden...</Text>
        </Box>
      ) : error ? (
        <Text color="red">{error}</Text>
      ) : (
        <Box flexDirection="column">
          <Text>{content || 'Kein Inhalt'}</Text>
        </Box>
      )}

      <Box marginTop={1}>
        <Text dimColor>Drücke Esc zum Zurück</Text>
      </Box>
    </Box>
  );
};