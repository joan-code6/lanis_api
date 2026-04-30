import React from 'react';
import { Box, Text } from 'ink';
import { useInput } from 'ink';
import SelectInput from 'ink-select-input';
import { CourseInfo } from '../types';

interface CoursesViewProps {
  courses: CourseInfo[];
  isLoading: boolean;
  error?: string;
  onBack: () => void;
  onSelectCourse: (course: CourseInfo) => void;
}

export const CoursesView: React.FC<CoursesViewProps> = ({
  courses,
  isLoading,
  error,
  onBack,
  onSelectCourse,
}) => {
  useInput((input, key) => {
    if (key.escape) {
      onBack();
    }
  });

  if (isLoading) {
    return (
      <Box flexDirection="column" padding={1}>
        <Text color="yellow">⟳ Kurse werden geladen...</Text>
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

  const courseItems = courses.map((course) => {
    const hasHomework = course.homework && course.homework.length > 0;
    const homeworkStatus = course.homeworkDone ? '✓' : '○';
    const topic = course.topic ? ` - ${course.topic.substring(0, 40)}` : '';
    const label = `${course.name.substring(0, 25).padEnd(25)} ${course.teacher.substring(0, 15).padEnd(15)} ${hasHomework ? `📝${homeworkStatus}` : ''}${topic}`;
    return { label, value: course.id, course };
  });

  courseItems.push({ label: '← Zurück zum Menü', value: 'back', course: undefined as any });

  const handleSelect = (item: { value: string; course?: CourseInfo }) => {
    if (item.value === 'back') {
      onBack();
    } else if (item.course) {
      onSelectCourse(item.course);
    }
  };

  return (
    <Box flexDirection="column" padding={1} borderStyle="round" borderColor="yellow">
      <Box marginBottom={1}>
        <Text bold color="yellow">
          🎓 MEIN UNTERRICHT
        </Text>
      </Box>

      <Box marginBottom={1}>
        <Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>
      </Box>

      <Box marginBottom={1}>
        <SelectInput items={courseItems} onSelect={handleSelect} />
      </Box>

      <Box marginTop={1}>
        <Text dimColor>{courses.length} Kurse • ↑↓ zum Navigieren</Text>
      </Box>
    </Box>
  );
};
