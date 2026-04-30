import React, { useState, useEffect } from 'react';
import { Box, Text } from 'ink';
import { LoginScreen } from './components/LoginScreen';
import { LoadingScreen } from './components/LoadingScreen';
import { Dashboard } from './components/Dashboard';
import { MessagesView } from './components/MessagesView';
import { MessageDetailView } from './components/MessageDetailView';
import { CalendarView } from './components/CalendarView';
import { CoursesView } from './components/CoursesView';
import { CourseDetailView } from './components/CourseDetailView';
import { ProfileView } from './components/ProfileView';
import { api } from './api';
import { storage } from './storage';
import { Screen, SessionState, LoginCredentials, Message, CalendarEvent, CourseInfo } from './types';

const App: React.FC = () => {
  const [screen, setScreen] = useState<Screen>('login');
  const [session, setSession] = useState<SessionState | null>(null);
  const [loginError, setLoginError] = useState<string>('');
  const [isLoading, setIsLoading] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [calendarEvents, setCalendarEvents] = useState<CalendarEvent[]>([]);
  const [courses, setCourses] = useState<CourseInfo[]>([]);
  const [profile, setProfile] = useState<any>(null);
  const [error, setError] = useState<string>('');
  const [autoLoginError, setAutoLoginError] = useState<string>('');
  const [selectedMessage, setSelectedMessage] = useState<Message | null>(null);
  const [selectedCourse, setSelectedCourse] = useState<CourseInfo | null>(null);

  // Auto-login on startup if credentials exist
  useEffect(() => {
    const saved = storage.getCredentials();
    if (saved) {
      setScreen('loading');
      handleLogin(saved, true);
    }
  }, []);

  const handleLogin = async (credentials: LoginCredentials, isAutoLogin = false) => {
    if (!isAutoLogin) {
      setIsLoading(true);
      setLoginError('');
    }

    try {
      const response = await api.login(credentials);
      setSession(response);
      api.setToken(response.token);

      // Always save credentials after successful login
      storage.saveCredentials(credentials);

      setScreen('dashboard');
      await loadDashboardData();
      setAutoLoginError('');
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Login failed';
      if (isAutoLogin) {
        setAutoLoginError(errorMsg);
        setScreen('login');
      } else {
        setLoginError(errorMsg);
      }
    } finally {
      if (!isAutoLogin) {
        setIsLoading(false);
      }
    }
  };

  const loadDashboardData = async () => {
    try {
      const [msgs, events, courseList] = await Promise.all([
        api.getMessages('all').catch(() => []),
        api.getCalendarEvents().catch(() => []),
        api.getCourses().catch(() => []),
      ]);

      setMessages(msgs);
      setCalendarEvents(events);
      setCourses(courseList);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load data');
    }
  };

  const handleLogout = async () => {
    try {
      await api.logout();
      setSession(null);
      setMessages([]);
      setCalendarEvents([]);
      setCourses([]);
      setProfile(null);
      setScreen('login');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Logout failed');
    }
  };

  const handleSelectModule = async (module: string) => {
    switch (module) {
      case 'messages':
        setScreen('messages');
        break;
      case 'calendar':
        setScreen('calendar');
        break;
      case 'courses':
        setScreen('courses');
        break;
      case 'profile':
        setScreen('profile');
        if (!profile) {
          setIsLoading(true);
          try {
            const profileData = await api.getUserProfile();
            setProfile(profileData);
          } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed to load profile');
          } finally {
            setIsLoading(false);
          }
        }
        break;
      default:
        setScreen('dashboard');
    }
  };

  const handleBack = () => {
    setSelectedMessage(null);
    setSelectedCourse(null);
    setScreen('dashboard');
  };

  const handleSelectMessage = (message: Message) => {
    setSelectedMessage(message);
  };

  const handleSelectCourse = (course: CourseInfo) => {
    setSelectedCourse(course);
  };

  return (
    <Box flexDirection="column">
      {screen === 'loading' && (
        <LoadingScreen error={autoLoginError} />
      )}

      {screen === 'login' && (
        <LoginScreen
          onSubmit={(creds) => handleLogin(creds, false)}
          isLoading={isLoading}
          error={loginError || autoLoginError}
        />
      )}

      {screen === 'dashboard' && session && (
        <Dashboard
          session={session}
          unreadMessages={messages.length}
          upcomingEvents={calendarEvents.length}
          onSelectModule={handleSelectModule}
          onLogout={handleLogout}
        />
      )}

      {screen === 'messages' && selectedMessage && (
        <MessageDetailView
          message={selectedMessage}
          onBack={() => setSelectedMessage(null)}
        />
      )}

      {screen === 'messages' && !selectedMessage && (
        <MessagesView
          messages={messages}
          isLoading={isLoading}
          error={error}
          onSelectMessage={handleSelectMessage}
          onBack={handleBack}
        />
      )}

      {screen === 'calendar' && (
        <CalendarView
          events={calendarEvents}
          isLoading={isLoading}
          error={error}
          onBack={handleBack}
        />
      )}

      {screen === 'courses' && selectedCourse && (
        <CourseDetailView
          course={selectedCourse}
          onBack={() => setSelectedCourse(null)}
        />
      )}

      {screen === 'courses' && !selectedCourse && (
        <CoursesView
          courses={courses}
          isLoading={isLoading}
          error={error}
          onBack={handleBack}
          onSelectCourse={handleSelectCourse}
        />
      )}

      {screen === 'profile' && session && (
        <ProfileView
          session={session}
          profile={profile}
          isLoading={isLoading}
          error={error}
          onBack={handleBack}
        />
      )}
    </Box>
  );
};

export default App;
