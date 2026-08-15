export type Category = 'all' | 'banks' | 'finance' | 'mutual_funds' | 'stocks' | 'search';

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

export interface DataPoint {
  entity: string;
  metric: string;
  value: string | number;
  unit: string;
  period: string;
  sourceTitle: string;
  sourceUrl: string;
  kind: 'live' | 'web';
}

export interface DataSearchResult {
  query: string;
  dataPoints: DataPoint[];
  sources: { title: string; url: string }[];
  sourceCount: number;
  generatedAt: string;
}
