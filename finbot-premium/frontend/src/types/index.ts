export type Category = 'all' | 'banks' | 'finance' | 'mutual_funds' | 'stocks';

export interface Article {
  id: string;
  category: string;
  title: string;
  source: string;
  url: string;
  time: string;
  tag: string;
  summary: string;
  image?: string;
  source_url?: string;  // real publisher URL when url is a Google News proxy
}

export interface Stock {
  symbol: string;
  price: string;
  change: string;
  up: boolean;
}

export interface QuickStat {
  label: string;
  value: string;
  sub: string;
}
