export interface LoginCredentials {
  schoolId: string;
  username: string;
  password: string;
}

export interface SessionState {
  token: string;
  schoolId: string;
  username: string;
  encryptionReady: boolean;
}

export interface AppModule {
  name: string;
  url: string;
  color?: string;
  logo?: string;
}

export interface Message {
  id: string;
  sender: string;
  subject: string;
  timestamp?: string;
  content?: string;
  date?: string;
  raw?: any;
}

export interface CalendarEvent {
  id: string;
  title: string;
  date: string;
  time?: string;
  description?: string;
}

export interface CourseInfo {
  id: string;
  name: string;
  teacher: string;
  topic?: string;
  date?: string;
  homework?: string;
  homeworkDone?: boolean;
  courseLink?: string;
}

export type Screen = 'login' | 'dashboard' | 'messages' | 'calendar' | 'courses' | 'profile' | 'loading';
