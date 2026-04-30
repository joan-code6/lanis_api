import React, { useState, useEffect, useCallback } from 'react';
import { Box, Text } from 'ink';
import { useInput } from 'ink';
import Spinner from 'ink-spinner';
import { api } from '../api';
import { CourseInfo } from '../types';

interface CourseDetailViewProps {
  course: CourseInfo;
  onBack: () => void;
}

interface CourseEntry {
  entry_id: string;
  date: string;
  hours: string;
  thema: string;
  homework: string;
  homework_done: boolean;
}

const PAGE_SIZE = 6;

export const CourseDetailView: React.FC<CourseDetailViewProps> = ({
  course,
  onBack,
}) => {
  const [entries, setEntries] = useState<CourseEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [page, setPage] = useState(0);
  const [toggling, setToggling] = useState<number | null>(null);

  const loadDetails = useCallback(async () => {
    console.log('[CourseDetail] Loading details for course ID:', course.id);
    try {
      const result = await api.getCourseDetails(course.id);
      console.log('[CourseDetail] API result:', result);
      
      if (result?.success === false) {
        console.error('[CourseDetail] API error:', result.error);
      } else if (result?.entries && Array.isArray(result.entries)) {
        setEntries(result.entries);
      } else if (result?.history && Array.isArray(result.history)) {
        setEntries(result.history);
      }
    } catch (err) {
      console.error('Error loading course details:', err);
    }
    setIsLoading(false);
  }, [course.id]);

  useEffect(() => {
    loadDetails();
  }, [loadDetails]);

  const handleToggleHomework = async (entry: CourseEntry, idx: number) => {
    if (!entry.homework || toggling !== null) return;
    
    setToggling(idx);
    const newDone = !entry.homework_done;
    const success = await api.setHomeworkDone(course.id, entry.entry_id, newDone);
    
    if (success) {
      const updated = entries.map((e, i) => 
        i === idx ? { ...e, homework_done: newDone } : e
      );
      setEntries(updated);
    }
    setToggling(null);
  };

  useInput((input, key) => {
    if (key.escape) {
      onBack();
    } else if (key.downArrow) {
      const totalPages = Math.ceil(entries.length / PAGE_SIZE);
      if (selectedIndex < entries.length - 1) {
        const newIndex = selectedIndex + 1;
        setSelectedIndex(newIndex);
        const newPage = Math.floor(newIndex / PAGE_SIZE);
        if (newPage !== page) setPage(newPage);
      }
    } else if (key.upArrow) {
      if (selectedIndex > 0) {
        const newIndex = selectedIndex - 1;
        setSelectedIndex(newIndex);
        const newPage = Math.floor(newIndex / PAGE_SIZE);
        if (newPage !== page) setPage(newPage);
      }
    } else if (key.return) {
      const globalIdx = page * PAGE_SIZE + selectedIndex;
      const entry = entries[globalIdx];
      if (entry) {
        handleToggleHomework(entry, globalIdx);
      }
    }
  });

  const displayedEntries = entries.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const totalPages = Math.ceil(entries.length / PAGE_SIZE);
  const currentStartIdx = page * PAGE_SIZE;

  return (
    <Box flexDirection="column" padding={1} borderStyle="round" borderColor="yellow" height={40}>
      <Box marginBottom={1}>
        <Text bold color="yellow">
          🎓 KURS DETAILS
        </Text>
      </Box>

      <Box marginBottom={1}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      <Box flexDirection="column" marginBottom={1}>
        <Text bold>Name: <Text color="cyan">{course.name}</Text></Text>
        <Text bold>Lehrer: <Text color="cyan">{course.teacher}</Text></Text>
      </Box>

      <Box marginBottom={1}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      {isLoading ? (
        <Box>
          <Spinner />
          <Text> Lade Verlauf...</Text>
        </Box>
      ) : entries.length > 0 ? (
        <Box flexDirection="column">
          <Text bold>📚 Verlauf ({entries.length} Einträge):</Text>
          {displayedEntries.map((entry, idx) => {
            const globalIdx = currentStartIdx + idx;
            const isSelected = globalIdx === selectedIndex;
            const cleanHomework = entry.homework 
              ? entry.homework.replace(/\n/g, ' ').replace(/\s+/g, ' ').trim().substring(0, 50)
              : '';
            const isToggling = toggling === globalIdx;
            
            return (
              <Box 
                key={globalIdx} 
                flexDirection="column" 
                marginTop={1}
                borderStyle={isSelected ? 'round' : undefined}
                borderColor={isSelected ? 'cyan' : undefined}
                paddingLeft={isSelected ? 1 : 0}
              >
                <Text dimColor>{entry.date} {entry.hours ? `(${entry.hours})` : ''}</Text>
                <Text bold={isSelected}>{entry.thema || 'Kein Thema'}</Text>
                {cleanHomework && (
                  <Text 
                    color={entry.homework_done ? 'green' : 'yellow'}
                    bold={isSelected}
                  >
                    {isToggling ? '⟳ ' : '📝 '}{cleanHomework} {entry.homework_done ? '✓' : '○'}
                  </Text>
                )}
                {isSelected && cleanHomework && (
                  <Text dimColor>Enter drücken zum Umschalten</Text>
                )}
              </Box>
            );
          })}
          
          <Box marginTop={1} flexDirection="column">
            <Text dimColor>Seite {page + 1}/{totalPages} | ↑↓ navigieren | Enter=Erledigt</Text>
          </Box>
        </Box>
      ) : (
        <Text dimColor>Kein Verlauf verfügbar</Text>
      )}

      <Box marginTop={1}>
        <Text dimColor>Drücke Esc zum Zurück</Text>
      </Box>
    </Box>
  );
};