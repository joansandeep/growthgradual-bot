import { Stock, QuickStat, Category } from '@/types';

export const CATEGORIES: { id: Category; label: string; icon: string }[] = [
  { id: 'all',          label: 'All Markets',          icon: '◆' },
  { id: 'stocks',       label: 'Stocks',               icon: '▲' },
  { id: 'banks',        label: 'Banking',              icon: '◈' },
  { id: 'finance',      label: 'Finance',              icon: '◇' },
  { id: 'mutual_funds', label: 'Mutual Funds',         icon: '●' },
];

// NOTE: These are loading placeholders — all prices are intentionally blank
// so the UI shows "—" until /api/market responds with live data.
// No hardcoded prices here; live values come from /api/market (NSE India + Stooq).
export const STOCKS: Stock[] = [
  { symbol: 'NIFTY 50',    price: '—', change: '—', up: true  },
  { symbol: 'SENSEX',      price: '—', change: '—', up: true  },
  { symbol: 'NIFTY BANK',  price: '—', change: '—', up: true  },
  { symbol: 'NIFTY IT',    price: '—', change: '—', up: true  },
  { symbol: 'NIFTY MID',   price: '—', change: '—', up: true  },
  { symbol: 'USD/INR',     price: '—', change: '—', up: false },
  { symbol: 'RELIANCE',    price: '—', change: '—', up: true  },
  { symbol: 'TCS',         price: '—', change: '—', up: true  },
  { symbol: 'HDFC BANK',   price: '—', change: '—', up: true  },
  { symbol: 'INFOSYS',     price: '—', change: '—', up: true  },
  { symbol: 'ICICI BANK',  price: '—', change: '—', up: true  },
  { symbol: 'WIPRO',       price: '—', change: '—', up: false },
];

export const QUICK_STATS: QuickStat[] = [
  { label: 'USD/INR',    value: '—', sub: 'Loading…' },
  { label: 'Gold (MCX)', value: '—', sub: 'Loading…' },
  { label: 'Crude Oil',  value: '—', sub: 'Loading…' },
  { label: 'Repo Rate',  value: '—', sub: 'Loading…' },
  { label: 'GDP',        value: '—', sub: 'Loading…' },
];

// Light theme tag colors: background color (text color handled in components)
export const TAG_COLORS: Record<string, string> = {
  Markets: '#dbeafe', Economy: '#dcfce7', Macro: '#dcfce7',
  Stocks: '#dbeafe', Global: '#e0e7ff', Banking: '#dbeafe',
  Finance: '#dbeafe', 'Mutual Funds': '#f3e8ff', NAV: '#dcfce7',
  Policy: '#fee2e2', Regulatory: '#ffedd5', Analysis: '#fef9c3',
  Research: '#fef3c7', Results: '#dcfce7', Guide: '#dbeafe',
  Rating: '#fee2e2', Data: '#cffafe', Technical: '#dcfce7',
  Picks: '#ede9fe', NFO: '#f3e8ff', 'Mutual Funds ': '#f3e8ff',
};
