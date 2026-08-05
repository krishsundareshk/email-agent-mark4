import React, { useState, useEffect } from 'react';
import { 
  Inbox, 
  Calendar, 
  BarChart2, 
  Settings, 
  RefreshCw, 
  ShieldAlert, 
  CheckCircle, 
  ChevronRight, 
  X, 
  LogOut, 
  User, 
  Mail,
  AlertTriangle,
  Clock,
  Compass,
  FileText,
  Pencil,
  ExternalLink,
  Users
} from 'lucide-react';

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

// Relationship replaces the old Priority dimension entirely (specs v3 §1 —
// Priority was removed). Colors are chosen for quick visual scanning, not
// as an urgency signal — Relationship isn't ordered the way Priority was.
const RELATIONSHIP_COLORS = {
  Internal: 'hsl(210, 80%, 60%)',
  Client: 'hsl(160, 70%, 45%)',
  Vendor: 'hsl(270, 60%, 65%)',
  'Automated-System': 'hsl(200, 15%, 55%)',
  Promotional: 'hsl(45, 90%, 55%)',
  'Unknown-External': 'hsl(30, 15%, 55%)',
  Suspicious: 'hsl(355, 85%, 60%)',
};

const CONFIDENCE_TIER_LABELS = {
  'auto-applied': 'Auto-applied',
  'needs-review': 'Needs Review',
  'unclassified': 'Unclassified',
};

const RELATIONSHIP_OPTIONS = ['Internal', 'Client', 'Vendor', 'Automated-System', 'Promotional', 'Unknown-External', 'Suspicious'];

const CHART_PIE_COLORS = ['#ef4444', '#f59e0b', '#10b981'];

export default function App() {
  // Authentication & Session
  const [token, setToken] = useState(
    sessionStorage.getItem('google_access_token') || sessionStorage.getItem('outlook_access_token') || ''
  );
  const [user, setUser] = useState(null);
  const [authError, setAuthError] = useState('');
  const [selectedTab, setSelectedTab] = useState('dashboard');
  const [emailInput, setEmailInput] = useState(localStorage.getItem('user_email_input') || '');
  const [clientId, setClientId] = useState('');
  const [outlookClientId, setOutlookClientId] = useState('');
  const [loginProvider, setLoginProvider] = useState('gmail'); // 'gmail' | 'outlook'

  // Database Data
  const [emails, setEmails] = useState([]);
  const [meetings, setMeetings] = useState([]);
  const [latestBatch, setLatestBatch] = useState(null);
  const [models, setModels] = useState([]);
  const [selectedModel, setSelectedModel] = useState(localStorage.getItem('selected_model') || 'qwen3:8b');

  // Interactive UI state
  const [syncing, setSyncing] = useState(false);
  const [banner, setBanner] = useState(null);
  const [selectedEmail, setSelectedEmail] = useState(null);

  // Email Filters — Relationship replaces Priority (specs v3 §1)
  const [filterRelationship, setFilterRelationship] = useState('All');
  const [filterConfidenceTier, setFilterConfidenceTier] = useState('All');

  // Human-correction feedback loop UI (specs v3 §5.4)
  const [correctingEmailId, setCorrectingEmailId] = useState(null);

  // Analytics tab data (specs v3 §8)
  const [analytics, setAnalytics] = useState(null);
  const [analyticsLoading, setAnalyticsLoading] = useState(false);

  // Fetch auth config, handle an Outlook redirect callback, and verify any existing session
  useEffect(() => {
    fetchAuthConfig();

    // Microsoft implicit-flow redirect lands back here with the token in
    // the URL fragment (specs v3 §9.2 — Outlook is the second email provider).
    if (window.location.hash.includes('access_token')) {
      const params = new URLSearchParams(window.location.hash.slice(1));
      const outlookToken = params.get('access_token');
      if (outlookToken) {
        window.history.replaceState(null, '', window.location.pathname);
        setToken(outlookToken);
        sessionStorage.setItem('outlook_access_token', outlookToken);
        fetchUserProfile(outlookToken, 'outlook');
        return;
      }
    }

    if (sessionStorage.getItem('outlook_access_token')) {
      fetchUserProfile(sessionStorage.getItem('outlook_access_token'), 'outlook');
    } else if (token) {
      fetchUserProfile(token, 'gmail');
    }
  }, []);

  const fetchAuthConfig = async () => {
    try {
      const resp = await fetch(`${API_BASE}/api/auth/config`);
      if (resp.ok) {
        const data = await resp.json();
        if (data.client_id) {
          setClientId(data.client_id);
        } else {
          setAuthError('No Google Client ID configured on the backend. Please verify your config.');
        }
      }
      const outlookResp = await fetch(`${API_BASE}/api/auth/outlook-config`);
      if (outlookResp.ok) {
        const outlookData = await outlookResp.json();
        setOutlookClientId(outlookData.client_id || '');
      }
    } catch (e) {
      console.error(e);
      setAuthError('Backend server unreachable.');
    }
  };

  // Load dashboard data when user is loaded
  useEffect(() => {
    if (user) {
      fetchDashboardData();
      fetchOllamaModels();
    }
  }, [user]);

  // Load analytics data on-demand when the Analytics tab is opened
  useEffect(() => {
    if (user && selectedTab === 'analytics') {
      fetchAnalytics();
    }
  }, [user, selectedTab]);

  // Auto close banners after 5 seconds
  useEffect(() => {
    if (banner) {
      const timer = setTimeout(() => setBanner(null), 5000);
      return () => clearTimeout(timer);
    }
  }, [banner]);

  const showBanner = (type, message) => {
    setBanner({ type, message });
  };

  const authHeaders = (includeToken = true) => {
    const headers = { 'X-User-Id': user?.user_id || user?.email || '' };
    if (includeToken && token) {
      if (loginProvider === 'outlook') {
        headers['X-Outlook-Token'] = token;
      } else {
        headers['Authorization'] = `Bearer ${token}`;
      }
    }
    return headers;
  };

  const fetchUserProfile = async (accessToken, provider = 'gmail') => {
    try {
      const headers = provider === 'outlook'
        ? { 'X-Outlook-Token': accessToken }
        : { 'Authorization': `Bearer ${accessToken}`, 'X-Google-Token': accessToken };
      const resp = await fetch(`${API_BASE}/api/user/profile`, { headers });
      if (resp.ok) {
        const data = await resp.json();
        setUser(data);
        setLoginProvider(provider);
        setAuthError('');
      } else {
        const err = await resp.text();
        console.error("Auth error response:", err);
        handleLogout();
        try {
          const errJson = JSON.parse(err);
          setAuthError(`Profile verification failed: ${errJson.detail || err}`);
        } catch {
          setAuthError(`Profile verification failed: ${err}`);
        }
      }
    } catch (e) {
      console.error(e);
      setAuthError('Backend server unreachable.');
    }
  };

  const handleGoogleLogin = () => {
    if (!emailInput) {
      setAuthError('Please enter your Gmail address first.');
      return;
    }
    if (!clientId) {
      setAuthError('Google Client ID configuration is missing on the backend. Make sure your Web Client ID is set in .env or client_secret.json.');
      return;
    }

    localStorage.setItem('user_email_input', emailInput);

    try {
      setAuthError('');
      if (!window.google) {
        setAuthError('Google Identity SDK failed to load. Please refresh the page.');
        return;
      }
      
      const client = window.google.accounts.oauth2.initTokenClient({
        client_id: clientId.trim(),
        scope: 'https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile',
        callback: async (tokenResponse) => {
          if (tokenResponse.error) {
            setAuthError(`OAuth failed: ${tokenResponse.error_description || tokenResponse.error}`);
            return;
          }
          const accessToken = tokenResponse.access_token;
          setToken(accessToken);
          sessionStorage.setItem('google_access_token', accessToken);
          await fetchUserProfile(accessToken, 'gmail');
          showBanner('success', 'Logged in successfully!');
        },
      });
      client.requestAccessToken();
    } catch (e) {
      console.error(e);
      setAuthError('Failed to initialize Google login popup. Please verify client configuration.');
    }
  };


  const handleOutlookLogin = () => {
    if (!outlookClientId) {
      setAuthError('Microsoft Client ID configuration is missing on the backend (MICROSOFT_CLIENT_ID).');
      return;
    }
    localStorage.setItem('user_email_input', emailInput);
    const scope = encodeURIComponent('Mail.Read Mail.ReadWrite Calendars.ReadWrite offline_access User.Read');
    const redirectUri = encodeURIComponent(window.location.origin + window.location.pathname);
    const tenant = 'common';
    const authorizeUrl =
      `https://login.microsoftonline.com/${tenant}/oauth2/v2.0/authorize` +
      `?client_id=${encodeURIComponent(outlookClientId)}` +
      `&response_type=token&redirect_uri=${redirectUri}&scope=${scope}&response_mode=fragment`;
    window.location.href = authorizeUrl;
  };

  const handleLogout = () => {
    setToken('');
    setUser(null);
    setEmails([]);
    setMeetings([]);
    setLatestBatch(null);
    setLoginProvider('gmail');
    sessionStorage.removeItem('google_access_token');
    sessionStorage.removeItem('outlook_access_token');
  };

  const handleResetDatabase = async () => {
    if (!window.confirm("Are you sure you want to delete all email history, meeting invitations, and logs? This action is irreversible.")) {
      return;
    }
    try {
      showBanner('info', 'Resetting database tables...');
      const resp = await fetch(`${API_BASE}/api/user/reset`, {
        method: 'POST',
        headers: authHeaders(),
      });
      if (resp.ok) {
        showBanner('success', 'Database cleared! You can now run a fresh sync.');
        await fetchDashboardData();
      } else {
        const err = await resp.text();
        showBanner('error', `Failed to reset: ${err}`);
      }
    } catch (e) {
      console.error(e);
      showBanner('error', 'Reset failed due to server error.');
    }
  };

  const fetchDashboardData = async () => {
    if (!user) return;
    const headers = authHeaders();

    try {
      // 1. Fetch Emails
      const emailsResp = await fetch(`${API_BASE}/api/emails/`, { headers });
      if (emailsResp.ok) {
        const emailsData = await emailsResp.json();
        setEmails(emailsData);
      }

      // 2. Fetch Meetings
      const meetingsResp = await fetch(`${API_BASE}/api/meetings/pending`, { headers });
      if (meetingsResp.ok) {
        const meetingsData = await meetingsResp.json();
        setMeetings(meetingsData);
      }

      // 3. Fetch Latest Batch Log
      const batchResp = await fetch(`${API_BASE}/api/batch/latest`, { headers });
      if (batchResp.ok) {
        const batchData = await batchResp.json();
        setLatestBatch(batchData);
      }
    } catch (e) {
      console.error("Error fetching dashboard data:", e);
      showBanner('error', 'Error refreshing dashboard data.');
    }
  };

  const fetchOllamaModels = async () => {
    try {
      const resp = await fetch(`${API_BASE}/api/models`);
      if (resp.ok) {
        const data = await resp.json();
        setModels(data.models || []);
      }
    } catch (e) {
      console.error("Error listing Ollama models:", e);
    }
  };

  const handleSyncEmails = async () => {
    if (!user || syncing) return;
    setSyncing(true);
    showBanner('info', `Connecting to ${user.email_provider === 'outlook' ? 'Outlook' : 'Gmail'} & analyzing emails...`);

    try {
      const resp = await fetch(`${API_BASE}/api/batch/run`, {
        method: 'POST',
        headers: {
          ...authHeaders(),
          'Content-Type': 'application/json',
          'X-Ollama-Model': selectedModel,
        },
      });

      if (resp.ok) {
        const data = await resp.json();
        if (data.success) {
          showBanner('success', `Sync finished! Classified ${data.emails_classified} email(s) and detected ${data.meetings_detected} meeting(s).`);
        } else {
          showBanner('error', `Sync finished with issues: ${data.error_message || 'Unknown error'}`);
        }
        await fetchDashboardData();
      } else {
        const err = await resp.text();
        showBanner('error', `Sync failed: ${err}`);
      }
    } catch (e) {
      console.error(e);
      showBanner('error', 'Sync failed due to network error.');
    } finally {
      setSyncing(false);
    }
  };

  const handleConfirmMeeting = async (meetingId) => {
    if (!user) return;
    showBanner('info', 'Adding event to your calendar...');
    try {
      const resp = await fetch(`${API_BASE}/api/meetings/${meetingId}/confirm`, {
        method: 'POST',
        headers: authHeaders(),
      });
      const data = await resp.json();
      if (data.success) {
        showBanner('success', 'Meeting successfully added to your calendar!');
        await fetchDashboardData();
      } else {
        showBanner('error', `Failed to confirm meeting: ${data.error_message || 'Calendar error'}`);
      }
    } catch (e) {
      console.error(e);
      showBanner('error', 'Failed to confirm meeting due to server error.');
    }
  };

  const handleDismissMeeting = async (meetingId) => {
    if (!user) return;
    try {
      const resp = await fetch(`${API_BASE}/api/meetings/${meetingId}/dismiss`, {
        method: 'POST',
        headers: authHeaders(),
      });
      const data = await resp.json();
      if (data.success) {
        showBanner('success', 'Meeting invitation dismissed.');
        await fetchDashboardData();
      } else {
        showBanner('error', 'Failed to dismiss meeting.');
      }
    } catch (e) {
      console.error(e);
      showBanner('error', 'Server error dismissing meeting.');
    }
  };

  const fetchAnalytics = async () => {
    if (!user || analyticsLoading) return;
    setAnalyticsLoading(true);
    const headers = authHeaders();
    try {
      const endpoints = [
        'volume-trend', 'relationship-distribution', 'meeting-funnel',
        'needs-review-queue', 'top-senders', 'trust-tier-breakdown',
        'promotional-noise-ratio', 'label-accuracy-over-time',
        'reasoning-agreement-rate', 'suspicious-count',
      ];
      const results = await Promise.all(
        endpoints.map(async (ep) => {
          const resp = await fetch(`${API_BASE}/api/analytics/${ep}`, { headers });
          return resp.ok ? await resp.json() : null;
        })
      );
      const [
        volumeTrend, relationshipDistribution, meetingFunnel, needsReviewQueue,
        topSenders, trustTierBreakdown, promotionalNoiseRatio, labelAccuracyOverTime,
        reasoningAgreementRate, suspiciousCount,
      ] = results;
      setAnalytics({
        volumeTrend, relationshipDistribution, meetingFunnel, needsReviewQueue,
        topSenders, trustTierBreakdown, promotionalNoiseRatio, labelAccuracyOverTime,
        reasoningAgreementRate, suspiciousCount,
      });
    } catch (e) {
      console.error("Error fetching analytics:", e);
    } finally {
      setAnalyticsLoading(false);
    }
  };

  const handleCorrectLabel = async (emailId, correctedLabel) => {
    if (!user) return;
    try {
      const resp = await fetch(`${API_BASE}/api/emails/${emailId}/correct-label`, {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ corrected_label: correctedLabel }),
      });
      const data = await resp.json();
      if (resp.ok && data.success) {
        showBanner('success', `Label corrected to "${correctedLabel}". The agent's memory for this sender has been updated.`);
        setCorrectingEmailId(null);
        await fetchDashboardData();
      } else {
        showBanner('error', `Failed to correct label: ${data.detail || 'Unknown error'}`);
      }
    } catch (e) {
      console.error(e);
      showBanner('error', 'Failed to correct label due to a server error.');
    }
  };

  const handleUpdateProviders = async (patch) => {
    if (!user) return;
    try {
      const resp = await fetch(`${API_BASE}/api/user/providers`, {
        method: 'PATCH',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      const data = await resp.json();
      if (resp.ok && data.success) {
        setUser({ ...user, ...data });
        showBanner('success', 'Provider settings updated.');
      } else {
        showBanner('error', `Failed to update settings: ${data.detail || 'Unknown error'}`);
      }
    } catch (e) {
      console.error(e);
      showBanner('error', 'Failed to update settings due to a server error.');
    }
  };

  const handleModelChange = (modelName) => {
    setSelectedModel(modelName);
    localStorage.setItem('selected_model', modelName);
    showBanner('success', `AI model switched to ${modelName}`);
  };

  // Compute summary stats from current emails — Relationship + confidence_tier
  // replace the old priority/flagged_for_review pair (specs v3 §1, §4).
  const totalEmailsCount = emails.length;
  const suspiciousCount = emails.filter(e => e.relationship === 'Suspicious').length;
  const needsReviewCount = emails.filter(e => e.confidence_tier === 'needs-review').length;
  const pendingMeetingsCount = meetings.length;

  // Filtering emails
  const filteredEmails = emails.filter(email => {
    if (filterRelationship !== 'All' && email.relationship !== filterRelationship) return false;
    if (filterConfidenceTier !== 'All' && email.confidence_tier !== filterConfidenceTier) return false;
    return true;
  });

  // Time formatting helper
  const formatTimeAgo = (isoStr) => {
    if (!isoStr) return 'Never';
    try {
      const diff = new Date() - new Date(isoStr);
      const mins = Math.floor(diff / 60000);
      if (mins < 1) return 'just now';
      if (mins === 1) return '1 min ago';
      if (mins < 60) return `${mins} mins ago`;
      const hrs = Math.floor(mins / 60);
      if (hrs === 1) return '1 hour ago';
      if (hrs < 24) return `${hrs} hours ago`;
      return new Date(isoStr).toLocaleDateString();
    } catch {
      return 'Unknown';
    }
  };

  // Google Login Screen
  if (!user) {
    return (
      <div className="auth-container">
        <div className="glass-card auth-card" style={{ width: '480px' }}>
          <div className="auth-logo">📧</div>
          <h2>Email Agentic Organizer</h2>
          <p style={{ marginBottom: 24 }}>Stateless AI email classifier and analytics dashboard. Powered by {selectedModel}.</p>
          
          {authError && (
            <div className="notification-banner banner-error" style={{ marginBottom: 20 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <ShieldAlert size={18} />
                <span>{authError}</span>
              </div>
            </div>
          )}

          <div className="auth-input-group">
            <label className="auth-label">Gmail Address</label>
            <input 
              type="email" 
              className="auth-input" 
              placeholder="e.g. krish.ai.data.science@gmail.com" 
              value={emailInput}
              onChange={e => setEmailInput(e.target.value)}
            />
          </div>

          {/* Client ID loaded dynamically from backend config */}

          <button onClick={handleGoogleLogin} className="google-auth-btn">
            <svg width="18" height="18" viewBox="0 0 18 18">
              <path fill="#4285F4" d="M17.6 9.2c0-.6-.0-1.2-.1-1.8H9v3.4h4.8c-.2 1.1-.8 2-1.8 2.6v2.2h2.9c1.7-1.6 2.7-4 2.7-6.4z"/>
              <path fill="#34A853" d="M9 18c2.4 0 4.5-.8 6-2.2l-2.9-2.2c-.8.5-1.8.9-3.1.9-2.4 0-4.4-1.6-5.1-3.8H1.1v2.3C2.6 15.8 5.6 18 9 18z"/>
              <path fill="#FBBC05" d="M3.9 10.7c-.2-.5-.3-1.1-.3-1.7s.1-1.2.3-1.7V5H1.1C.4 6.4 0 8 0 9.7s.4 3.3 1.1 4.7l2.8-2.3z"/>
              <path fill="#EA4335" d="M9 3.6c1.3 0 2.5.4 3.4 1.3l2.6-2.6C13.4 1 11.4 0 9 0 5.6 0 2.6 2.2 1.1 5l2.8 2.3c.7-2.2 2.7-3.7 5.1-3.7z"/>
            </svg>
            <span>Authenticate with Google</span>
          </button>

          <button onClick={handleOutlookLogin} className="google-auth-btn" style={{ marginTop: 10 }}>
            <svg width="18" height="18" viewBox="0 0 23 23">
              <path fill="#f25022" d="M1 1h10v10H1z"/>
              <path fill="#00a4ef" d="M1 12h10v10H1z"/>
              <path fill="#7fba00" d="M12 1h10v10H12z"/>
              <path fill="#ffb900" d="M12 12h10v10H12z"/>
            </svg>
            <span>Authenticate with Outlook</span>
          </button>
        </div>
      </div>
    );
  }

  // Dashboard Shell
  return (
    <>
      {/* Sidebar navigation */}
      <div className="app-sidebar glass">
        <div>
          <div className="brand">
            <span>📧</span>
          </div>
          
          <div className="sidebar-nav">
            <div 
              className={`nav-item ${selectedTab === 'dashboard' ? 'active' : ''}`}
              onClick={() => setSelectedTab('dashboard')}
            >
              <BarChart2 size={18} />
              <span>Overview</span>
            </div>
            
            <div 
              className={`nav-item ${selectedTab === 'emails' ? 'active' : ''}`}
              onClick={() => setSelectedTab('emails')}
            >
              <Inbox size={18} />
              <span>Inbox Organizer</span>
              {suspiciousCount > 0 && (
                <span className="badge badge-high" style={{ marginLeft: 'auto', padding: '2px 6px', fontSize: '0.65rem' }}>
                  {suspiciousCount}
                </span>
              )}
            </div>
            
            <div 
              className={`nav-item ${selectedTab === 'meetings' ? 'active' : ''}`}
              onClick={() => setSelectedTab('meetings')}
            >
              <Calendar size={18} />
              <span>Meeting RSVPs</span>
              {pendingMeetingsCount > 0 && (
                <span className="badge badge-medium" style={{ marginLeft: 'auto', padding: '2px 6px', fontSize: '0.65rem' }}>
                  {pendingMeetingsCount}
                </span>
              )}
            </div>

            <div 
              className={`nav-item ${selectedTab === 'analytics' ? 'active' : ''}`}
              onClick={() => setSelectedTab('analytics')}
            >
              <BarChart2 size={18} />
              <span>Analytics</span>
            </div>
            
            <div 
              className={`nav-item ${selectedTab === 'settings' ? 'active' : ''}`}
              onClick={() => setSelectedTab('settings')}
            >
              <Settings size={18} />
              <span>Agent Settings</span>
            </div>
          </div>
        </div>

        {/* User Card info */}
        <div className="user-profile-section">
          {user.picture ? (
            <img src={user.picture} alt="avatar" className="profile-avatar" />
          ) : (
            <div className="profile-avatar" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(255,255,255,0.06)' }}>
              <User size={18} />
            </div>
          )}
          <div className="profile-info">
            <span className="profile-name">{user.name}</span>
            <span className="profile-email">{user.email}</span>
          </div>
          <button className="logout-btn" onClick={handleLogout} title="Log Out">
            <LogOut size={16} />
          </button>
        </div>
      </div>

      {/* Main Workspace content */}
      <div className="app-content">
        {/* Banner Messages */}
        {banner && (
          <div className={`notification-banner banner-${banner.type}`}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              {banner.type === 'success' && <CheckCircle size={18} />}
              {banner.type === 'error' && <ShieldAlert size={18} />}
              {banner.type === 'info' && <RefreshCw size={18} className="spinner" style={{ animationDuration: '2s' }} />}
              <span>{banner.message}</span>
            </div>
            <button style={{ background: 'transparent', border: 'none', color: 'inherit', cursor: 'pointer' }} onClick={() => setBanner(null)}>
              <X size={16} />
            </button>
          </div>
        )}

        {/* Dynamic tabs views */}
        {selectedTab === 'dashboard' && (
          <div>
            <div className="page-header">
              <div>
                <h1>Dashboard Overview</h1>
                <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginTop: 4 }}>
                  Classifying and scheduling with {selectedModel}
                </p>
              </div>
              <div style={{ display: 'flex', gap: 12 }}>
                <button className="btn btn-primary" onClick={handleSyncEmails} disabled={syncing}>
                  <RefreshCw size={16} className={syncing ? 'spinner' : ''} />
                  <span>Sync Gmail Agent</span>
                </button>
              </div>
            </div>

            <div className="glass-card minimal-status-card" style={{ marginBottom: 24 }}>
              <h3 style={{ fontSize: '1.1rem', marginBottom: 6 }}>Inbox Status Summary</h3>
              <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 24 }}>
                System is active and monitoring Gmail inbox.
              </p>
              
              <div className="minimal-stats-row" style={{ display: 'flex', flexWrap: 'wrap', gap: 40, marginBottom: 24 }}>
                <div className="minimal-stat-item">
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>Total Parsed</span>
                  <strong style={{ fontSize: '2.2rem', fontWeight: 700 }}>{totalEmailsCount}</strong>
                </div>
                <div className="minimal-stat-item">
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>Suspicious Flagged</span>
                  <strong style={{ fontSize: '2.2rem', fontWeight: 700, color: suspiciousCount > 0 ? 'var(--color-high)' : 'var(--text-primary)' }}>{suspiciousCount}</strong>
                </div>
                <div className="minimal-stat-item">
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>RSVP Invites</span>
                  <strong style={{ fontSize: '2.2rem', fontWeight: 700, color: pendingMeetingsCount > 0 ? 'var(--color-medium)' : 'var(--text-primary)' }}>{pendingMeetingsCount}</strong>
                </div>
                <div className="minimal-stat-item">
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>Needs Review</span>
                  <strong style={{ fontSize: '2.2rem', fontWeight: 700, color: needsReviewCount > 0 ? 'var(--color-flagged)' : 'var(--text-primary)' }}>{needsReviewCount}</strong>
                </div>
              </div>

              {latestBatch && latestBatch.found && (
                <div style={{ borderTop: '1px solid var(--glass-border)', paddingTop: 16, fontSize: '0.8rem', color: 'var(--text-muted)', display: 'flex', gap: 24, flexWrap: 'wrap' }}>
                  <span><strong>Last Sync:</strong> {formatTimeAgo(latestBatch.completed_at)}</span>
                  <span><strong>Status:</strong> {latestBatch.status}</span>
                  <span><strong>Classified:</strong> {latestBatch.emails_classified} new emails</span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Tab 2: Inbox Organizer */}
        {selectedTab === 'emails' && (
          <div>
            <div className="page-header">
              <div>
                <h1>Inbox Organizer</h1>
                <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginTop: 4 }}>
                  List of emails analyzed and processed by Qwen agent
                </p>
              </div>
              <button className="btn btn-primary" onClick={handleSyncEmails} disabled={syncing}>
                <RefreshCw size={16} className={syncing ? 'spinner' : ''} />
                <span>Sync Emails</span>
              </button>
            </div>

            {/* Filter Toolbar — Relationship + confidence tier replace Priority (specs v3 §1) */}
            <div className="filter-bar" style={{ display: 'flex', gap: 12, marginBottom: 24, flexWrap: 'wrap', alignItems: 'center' }}>
              <select
                value={filterRelationship}
                onChange={e => setFilterRelationship(e.target.value)}
                className="btn btn-secondary"
                style={{ padding: '8px 12px', fontSize: '0.85rem', borderRadius: '8px' }}
              >
                <option value="All">All Relationships</option>
                {RELATIONSHIP_OPTIONS.map(r => <option key={r} value={r}>{r}</option>)}
              </select>
              <button 
                className={`btn ${filterConfidenceTier === 'All' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setFilterConfidenceTier('All')}
                style={{ padding: '8px 16px', fontSize: '0.85rem', borderRadius: '8px' }}
              >
                All
              </button>
              <button 
                className={`btn ${filterConfidenceTier === 'needs-review' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setFilterConfidenceTier('needs-review')}
                style={{ padding: '8px 16px', fontSize: '0.85rem', borderRadius: '8px' }}
              >
                🔍 Needs Review
              </button>
              <button 
                className={`btn ${filterConfidenceTier === 'auto-applied' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setFilterConfidenceTier('auto-applied')}
                style={{ padding: '8px 16px', fontSize: '0.85rem', borderRadius: '8px' }}
              >
                ✅ Auto-applied
              </button>
            </div>

            {/* Emails List layout — sender + Relationship + Department + Meeting badge +
                timestamp + "Open in inbox" link only. No subject/summary rendered here:
                the backend never returns them for ordinary rows (specs v3 §6). */}
            <div className="email-list" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {filteredEmails.length === 0 ? (
                <div className="glass-card" style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>
                  No emails match the selected filters.
                </div>
              ) : (
                filteredEmails.map(email => (
                  <div 
                    key={email.email_id} 
                    className="glass-card glass-card-hover email-item"
                    style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '16px 20px', gap: 16, position: 'relative' }}
                  >
                    <div
                      style={{ display: 'flex', alignItems: 'center', gap: 14, flexGrow: 1, minWidth: 0, cursor: 'pointer' }}
                      onClick={() => setSelectedEmail(email)}
                    >
                      <div 
                        className="sender-initial" 
                        style={{ background: RELATIONSHIP_COLORS[email.relationship] || 'hsl(220,10%,50%)', width: 34, height: 34, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 600, flexShrink: 0, fontSize: '0.9rem' }}
                      >
                        {(email.sender_name ? email.sender_name[0] : (email.sender_email ? email.sender_email[0] : 'E')).toUpperCase()}
                      </div>
                      <div style={{ minWidth: 0, flexGrow: 1 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 2 }}>
                          <h4 style={{ margin: 0, fontSize: '0.9rem', fontWeight: 600 }}>{email.sender_name || email.sender_email || 'External Sender'}</h4>
                          <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{formatTimeAgo(email.processed_at)}</span>
                        </div>
                        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 4 }}>
                          <span className="badge" style={{ background: `${RELATIONSHIP_COLORS[email.relationship]}22`, color: RELATIONSHIP_COLORS[email.relationship], fontSize: '0.7rem', padding: '2px 8px', borderRadius: 6 }}>
                            {email.relationship}
                          </span>
                          {email.department && email.department !== 'General' && (
                            <span className="badge badge-dept" style={{ fontSize: '0.7rem' }}>{email.department}</span>
                          )}
                          {email.is_meeting && (
                            <span className="badge" style={{ fontSize: '0.7rem', padding: '2px 8px', borderRadius: 6, background: 'rgba(99,102,241,0.12)', color: '#818cf8' }}>
                              <Calendar size={10} style={{ marginRight: 3, verticalAlign: 'middle' }} />Meeting
                            </span>
                          )}
                          {email.confidence_tier === 'needs-review' && (
                            <span className="badge" style={{ fontSize: '0.7rem', padding: '2px 8px', borderRadius: 6, background: 'rgba(245,158,11,0.12)', color: 'var(--color-medium)' }}>
                              Needs Review
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                      <a
                        href={email.open_in_inbox_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={e => e.stopPropagation()}
                        className="btn btn-secondary"
                        style={{ padding: '6px 10px', fontSize: '0.75rem', borderRadius: 8, display: 'flex', alignItems: 'center', gap: 4 }}
                        title="Open in inbox"
                      >
                        <ExternalLink size={13} />
                      </a>
                      <button
                        className="btn btn-secondary"
                        style={{ padding: '6px 10px', fontSize: '0.75rem', borderRadius: 8, display: 'flex', alignItems: 'center', gap: 4 }}
                        title="This is mislabeled"
                        onClick={e => { e.stopPropagation(); setCorrectingEmailId(correctingEmailId === email.email_id ? null : email.email_id); }}
                      >
                        <Pencil size={13} />
                      </button>
                    </div>

                    {correctingEmailId === email.email_id && (
                      <div
                        className="glass-card"
                        style={{ position: 'absolute', top: '100%', right: 20, marginTop: 4, zIndex: 10, padding: 10, display: 'flex', flexDirection: 'column', gap: 4, minWidth: 180 }}
                        onClick={e => e.stopPropagation()}
                      >
                        <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginBottom: 4 }}>Correct label to:</span>
                        {RELATIONSHIP_OPTIONS.filter(r => r !== email.relationship).map(r => (
                          <button
                            key={r}
                            className="btn btn-secondary"
                            style={{ fontSize: '0.75rem', padding: '6px 10px', textAlign: 'left' }}
                            onClick={() => handleCorrectLabel(email.email_id, r)}
                          >
                            {r}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {/* Tab 3: Meeting RSVPs */}
        {selectedTab === 'meetings' && (
          <div>
            <div className="page-header">
              <div>
                <h1>Meeting Invites & RSVPs</h1>
                <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginTop: 4 }}>
                  Invites detected by the AI agent requiring calendar confirmations
                </p>
              </div>
            </div>

            {meetings.length === 0 ? (
              <div className="glass-card" style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>
                No pending meeting invitations. Great job!
              </div>
            ) : (
              <div className="meetings-grid">
                {meetings.map(meet => (
                  <div key={meet.meeting_id} className="glass-card meeting-card">
                    <div className="meeting-header">
                      <h3>{meet.meeting_title}</h3>
                      <div className="meeting-datetime">
                        <Clock size={14} />
                        <span>
                          {new Date(meet.meeting_datetime).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })} ({meet.duration_minutes} min)
                        </span>
                      </div>
                    </div>

                    <div className="meeting-details">
                      <div className="meeting-details-item">
                        <strong>Organizer:</strong>
                        <span>{meet.organizer_name || meet.organizer_email}</span>
                      </div>
                      {meet.location_or_link && (
                        <div className="meeting-details-item" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          <strong>Location:</strong>
                          <span>{meet.location_or_link}</span>
                        </div>
                      )}
                      {meet.attendees && meet.attendees.length > 0 && (
                        <div className="meeting-details-item">
                          <strong>Attendees:</strong>
                          <span>{meet.attendees.join(', ')}</span>
                        </div>
                      )}
                      <div className="meeting-purpose">
                        <strong>Purpose:</strong>
                        <p>{meet.meeting_summary}</p>
                      </div>
                    </div>

                    <div className="meeting-actions">
                      <button 
                        className="btn btn-secondary" 
                        onClick={() => handleDismissMeeting(meet.meeting_id)}
                      >
                        Dismiss
                      </button>
                      <button 
                        className="btn btn-primary"
                        onClick={() => handleConfirmMeeting(meet.meeting_id)}
                      >
                        Add to Calendar
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Tab: Analytics (specs v3 §8) */}
        {selectedTab === 'analytics' && (
          <div>
            <div className="page-header">
              <div>
                <h1>Analytics</h1>
                <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginTop: 4 }}>
                  Is the agent actually improving? — label accuracy, agreement rate, and volume trends
                </p>
              </div>
              <button className="btn btn-secondary" onClick={fetchAnalytics} disabled={analyticsLoading}>
                <RefreshCw size={16} className={analyticsLoading ? 'spinner' : ''} />
                <span>Refresh</span>
              </button>
            </div>

            {!analytics ? (
              <div className="glass-card" style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>
                {analyticsLoading ? 'Loading analytics…' : 'No analytics data yet.'}
              </div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 20 }}>

                {/* Label accuracy over time — the featured widget (specs v3 §8) */}
                <div className="glass-card" style={{ padding: 20, gridColumn: '1 / -1' }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 4 }}>Label Accuracy Over Time</h3>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: 16 }}>
                    Correction rate per week — should trend down as trust builds.
                  </p>
                  <BarRow
                    labels={analytics.labelAccuracyOverTime?.weeks || []}
                    values={analytics.labelAccuracyOverTime?.correction_rate || []}
                    format={v => `${(v * 100).toFixed(1)}%`}
                    color="var(--color-medium)"
                  />
                </div>

                {/* Relationship distribution */}
                <div className="glass-card" style={{ padding: 20 }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 12 }}>Relationship Distribution</h3>
                  {Object.entries(analytics.relationshipDistribution?.percentages || {}).map(([label, pct]) => (
                    <div key={label} style={{ marginBottom: 8 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8rem', marginBottom: 3 }}>
                        <span>{label}</span><span>{pct}%</span>
                      </div>
                      <div style={{ height: 6, borderRadius: 4, background: 'rgba(255,255,255,0.06)' }}>
                        <div style={{ height: '100%', width: `${pct}%`, borderRadius: 4, background: RELATIONSHIP_COLORS[label] || '#888' }} />
                      </div>
                    </div>
                  ))}
                </div>

                {/* Meeting funnel */}
                <div className="glass-card" style={{ padding: 20 }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 12 }}>Meeting Funnel</h3>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    <StatLine label="Detected" value={analytics.meetingFunnel?.detected} />
                    <StatLine label="Confirmed" value={analytics.meetingFunnel?.confirmed} />
                    <StatLine label="Dismissed" value={analytics.meetingFunnel?.dismissed} />
                    <StatLine label="Pending" value={analytics.meetingFunnel?.pending} />
                    <StatLine label="Upcoming" value={analytics.meetingFunnel?.upcoming} />
                  </div>
                </div>

                {/* Needs-review queue */}
                <div className="glass-card" style={{ padding: 20 }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 12 }}>Needs-Review Queue</h3>
                  <StatLine label="Queue size" value={analytics.needsReviewQueue?.queue_size} />
                  <StatLine
                    label="Avg. time to resolution"
                    value={analytics.needsReviewQueue?.avg_time_to_resolution_hours != null
                      ? `${analytics.needsReviewQueue.avg_time_to_resolution_hours}h` : '—'}
                  />
                </div>

                {/* Sender trust-tier breakdown */}
                <div className="glass-card" style={{ padding: 20 }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 12 }}>Sender Trust-Tier Breakdown</h3>
                  <StatLine label="New" value={analytics.trustTierBreakdown?.New} />
                  <StatLine label="Familiar" value={analytics.trustTierBreakdown?.Familiar} />
                  <StatLine label="Trusted" value={analytics.trustTierBreakdown?.Trusted} />
                </div>

                {/* Reasoning agreement rate */}
                <div className="glass-card" style={{ padding: 20 }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 12 }}>Reasoning Agreement Rate</h3>
                  <StatLine label="Confirmed" value={`${((analytics.reasoningAgreementRate?.confirmed_rate || 0) * 100).toFixed(1)}%`} />
                  <StatLine label="Revised" value={`${((analytics.reasoningAgreementRate?.revised_rate || 0) * 100).toFixed(1)}%`} />
                  <StatLine label="Reversed" value={`${((analytics.reasoningAgreementRate?.reversed_rate || 0) * 100).toFixed(1)}%`} />
                </div>

                {/* Suspicious count — kept visually prominent per specs v3 §8 */}
                <div className="glass-card" style={{ padding: 20, borderColor: 'rgba(239,68,68,0.3)' }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                    <AlertTriangle size={16} color="var(--color-high)" /> Suspicious-Flag Count
                  </h3>
                  <strong style={{ fontSize: '2.4rem', color: 'var(--color-high)' }}>
                    {analytics.suspiciousCount?.suspicious_count ?? 0}
                  </strong>
                </div>

                {/* Promotional noise ratio */}
                <div className="glass-card" style={{ padding: 20 }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 4 }}>Promotional Noise Ratio</h3>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: 12 }}>Share of daily volume that is Promotional</p>
                  <BarRow
                    labels={analytics.promotionalNoiseRatio?.days || []}
                    values={analytics.promotionalNoiseRatio?.ratio || []}
                    format={v => `${(v * 100).toFixed(0)}%`}
                    color="hsl(45, 90%, 55%)"
                  />
                </div>

                {/* Top senders */}
                <div className="glass-card" style={{ padding: 20 }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 12 }}>Top Senders by Volume</h3>
                  {(analytics.topSenders || []).map(s => (
                    <StatLine key={s.sender_email} label={s.sender_name || s.sender_email} value={s.count} />
                  ))}
                </div>

                {/* Volume trend */}
                <div className="glass-card" style={{ padding: 20, gridColumn: '1 / -1' }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: 4 }}>Volume Trend</h3>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: 16 }}>Emails processed per day</p>
                  <BarRow
                    labels={analytics.volumeTrend?.days || []}
                    values={analytics.volumeTrend?.series || []}
                    format={v => `${v}`}
                    color="var(--color-low)"
                  />
                </div>
              </div>
            )}
          </div>
        )}

        {/* Tab 4: Agent Settings */}
        {selectedTab === 'settings' && (
          <div className="settings-section">
            <div className="page-header">
              <div>
                <h1>Agent Settings</h1>
                <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginTop: 4 }}>
                  Configure Ollama parameters and model mappings
                </p>
              </div>
            </div>

            <div className="glass-card settings-card">
              <div className="settings-row">
                <label>Classification Model</label>
                <select 
                  value={selectedModel} 
                  onChange={e => handleModelChange(e.target.value)}
                  style={{ width: '100%' }}
                >
                  <option value="qwen3:8b">qwen3:8b (Active)</option>
                  {models.map(m => (
                    m.name !== "qwen3:8b" && <option key={m.name} value={m.name}>{m.name}</option>
                  ))}
                </select>
              </div>

              <div className="settings-row">
                <label>{loginProvider === 'outlook' ? 'Microsoft' : 'Google'} OAuth Status</label>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: 'rgba(255, 255, 255, 0.02)', border: '1px solid var(--glass-border)', padding: '12px 16px', borderRadius: 10, marginTop: 4 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <div style={{ width: 10, height: 10, borderRadius: '50%', background: 'var(--color-low)' }}></div>
                    <span>Active Session: {user.email}</span>
                  </div>
                  <button className="btn btn-secondary" style={{ padding: '6px 12px', fontSize: '0.8rem', marginLeft: 'auto' }} onClick={handleLogout}>
                    Disconnect
                  </button>
                </div>
              </div>

              <div className="settings-row" style={{ marginTop: 16 }}>
                <label>Email &amp; Calendar Providers</label>
                <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: 8 }}>
                  Exactly two of each are supported (specs v3 §9.2) — Gmail or Outlook for email,
                  Google Calendar or Outlook/Teams Calendar for scheduling.
                </p>
                <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                  <select
                    value={user.email_provider || 'gmail'}
                    onChange={e => handleUpdateProviders({ email_provider: e.target.value })}
                    className="auth-input"
                    style={{ flex: 1, minWidth: 160 }}
                  >
                    <option value="gmail">Gmail</option>
                    <option value="outlook">Outlook</option>
                  </select>
                  <select
                    value={user.calendar_provider || 'google'}
                    onChange={e => handleUpdateProviders({ calendar_provider: e.target.value })}
                    className="auth-input"
                    style={{ flex: 1, minWidth: 160 }}
                  >
                    <option value="google">Google Calendar</option>
                    <option value="outlook">Outlook / Teams Calendar</option>
                  </select>
                </div>
              </div>

              <div className="settings-row" style={{ marginTop: 16 }}>
                <label>Organization Domain(s)</label>
                <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: 8 }}>
                  Comma-separated. Used as a structural-corroboration signal for the Internal
                  relationship label (specs v3 §3, §4).
                </p>
                <input
                  type="text"
                  className="auth-input"
                  placeholder="e.g. mycompany.com, mycompany.io"
                  defaultValue={user.org_domains || ''}
                  onBlur={e => handleUpdateProviders({ org_domains: e.target.value })}
                />
              </div>

              <div className="settings-row">
                <label>Stateless Auth Mode</label>
                <div style={{ padding: 12, borderRadius: 10, background: 'rgba(99, 102, 241, 0.04)', border: '1px solid rgba(99, 102, 241, 0.15)', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                  🔐 Access tokens are held strictly in the browser session memory and are never written to the server's database or local files.
                </div>
              </div>

              <div className="settings-row" style={{ marginTop: 24 }}>
                <label style={{ color: 'var(--color-high)' }}>Database Control</label>
                <div style={{ padding: 12, borderRadius: 10, background: 'rgba(239, 68, 68, 0.03)', border: '1px solid rgba(239, 68, 68, 0.15)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>Delete all classified emails, meetings, and sync logs.</span>
                  <button 
                    className="btn btn-secondary" 
                    style={{ background: 'rgba(239, 68, 68, 0.08)', color: 'var(--color-high)', border: '1px solid rgba(239, 68, 68, 0.3)', padding: '8px 14px', borderRadius: 8, cursor: 'pointer', fontSize: '0.8rem', fontWeight: 600 }}
                    onClick={handleResetDatabase}
                  >
                    Reset Database
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Email Detail Modal Popup */}
      {selectedEmail && (
        <div className="modal-overlay" onClick={() => setSelectedEmail(null)}>
          <div className="glass-card modal-content" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <div style={{ width: '100%' }}>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
                  <span
                    className="badge"
                    style={{ background: `${RELATIONSHIP_COLORS[selectedEmail.relationship]}22`, color: RELATIONSHIP_COLORS[selectedEmail.relationship], fontWeight: 600 }}
                  >
                    {selectedEmail.relationship}
                  </span>
                  <span className="badge badge-dept">
                    {selectedEmail.department} Dept
                  </span>
                  {selectedEmail.is_meeting && (
                    <span className="badge" style={{ background: 'rgba(99,102,241,0.12)', color: '#818cf8' }}>Meeting</span>
                  )}
                </div>
                <h2 style={{ fontSize: '1.3rem', fontWeight: 700, margin: '8px 0', lineHeight: 1.3 }}>
                  {selectedEmail.sender_name || selectedEmail.sender_email || 'Unknown sender'}
                </h2>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 12, borderTop: '1px solid var(--glass-border)', paddingTop: 12 }}>
                  <div style={{ fontSize: '0.9rem', color: 'var(--text-primary)' }}>
                    <strong>From:</strong> {selectedEmail.sender_name ? `${selectedEmail.sender_name} <${selectedEmail.sender_email}>` : selectedEmail.sender_email || 'Unknown'}
                  </div>
                  <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                    <strong>Message ID:</strong> {selectedEmail.email_id}
                  </div>
                </div>
              </div>
              <button className="modal-close-btn" onClick={() => setSelectedEmail(null)}>
                <X size={20} />
              </button>
            </div>

            <div className="modal-meta-row">
              <div className="modal-meta-card">
                <span>Confidence Tier</span>
                <strong style={{ color: selectedEmail.confidence_tier === 'auto-applied' ? 'var(--color-low)' : 'var(--color-medium)' }}>
                  {CONFIDENCE_TIER_LABELS[selectedEmail.confidence_tier] || selectedEmail.confidence_tier}
                </strong>
              </div>
              <div className="modal-meta-card">
                <span>Self-Reported Certainty</span>
                <strong>{((selectedEmail.self_reported_certainty || 0) * 100).toFixed(0)}%</strong>
              </div>
              <div className="modal-meta-card">
                <span>Suspicious</span>
                <strong style={{ color: selectedEmail.relationship === 'Suspicious' ? 'var(--color-high)' : 'var(--color-low)' }}>
                  {selectedEmail.relationship === 'Suspicious' ? 'Yes' : 'No'}
                </strong>
              </div>
            </div>

            {/* No subject/summary/body is ever fetched or stored (specs v3 §6) —
                the deep link below is the way to see the actual content. */}
            <div className="modal-body">
              <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: 16 }}>
                Message content isn't stored by the agent — sender identity, labels, and
                timestamp only. Open the message directly to read it.
              </p>

              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                <a
                  href={selectedEmail.open_in_inbox_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="btn btn-primary"
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6, textDecoration: 'none' }}
                >
                  <ExternalLink size={14} /> Open in Inbox
                </a>
                <button
                  className="btn btn-secondary"
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
                  onClick={() => setCorrectingEmailId(selectedEmail.email_id)}
                >
                  <Pencil size={14} /> This is mislabeled
                </button>
              </div>

              {correctingEmailId === selectedEmail.email_id && (
                <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>Correct label to:</span>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    {RELATIONSHIP_OPTIONS.filter(r => r !== selectedEmail.relationship).map(r => (
                      <button
                        key={r}
                        className="btn btn-secondary"
                        style={{ fontSize: '0.8rem', padding: '8px 12px' }}
                        onClick={async () => {
                          await handleCorrectLabel(selectedEmail.email_id, r);
                          setSelectedEmail(null);
                        }}
                      >
                        {r}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              <div style={{ marginTop: 24, fontSize: '0.8rem', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
                <Clock size={12} />
                <span>Processed at {new Date(selectedEmail.processed_at).toLocaleString()}</span>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Small presentational helpers for the Analytics tab (specs v3 §8).
// Deliberately dependency-free (no chart library) — simple CSS bars are
// enough for these widgets and keep the frontend bundle lean.
// ─────────────────────────────────────────────────────────────────────────

function StatLine({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.85rem', padding: '4px 0' }}>
      <span style={{ color: 'var(--text-secondary)' }}>{label}</span>
      <strong>{value ?? 0}</strong>
    </div>
  );
}

function BarRow({ labels, values, format, color }) {
  if (!labels || labels.length === 0) {
    return <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>No data yet.</div>;
  }
  const max = Math.max(...values, 0.0001);
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 120, overflowX: 'auto' }}>
      {labels.map((label, i) => {
        const value = values[i] || 0;
        const heightPct = Math.max(2, (value / max) * 100);
        return (
          <div key={label} title={`${label}: ${format ? format(value) : value}`} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', minWidth: 22, flexShrink: 0 }}>
            <div style={{ width: 14, height: `${heightPct}%`, background: color, borderRadius: 3, marginBottom: 4 }} />
            <span style={{ fontSize: '0.55rem', color: 'var(--text-muted)', writingMode: 'vertical-rl', transform: 'rotate(180deg)', height: 40 }}>
              {label.length > 10 ? label.slice(5) : label}
            </span>
          </div>
        );
      })}
    </div>
  );
}
