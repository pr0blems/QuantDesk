export type SchemaNode = {
  type: string;
  properties?: Record<string, SchemaNode>;
  items?: SchemaNode;
};

export type QueryParam = {
  name: string;
  type: string;
  required: boolean;
  example: unknown;
  description: string;
  context: boolean;
};

export type Endpoint = {
  id: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  host: string;
  path: string;
  calls: number;
  statuses: Array<{ status: number; count: number }>;
  usable: boolean;
  category: string;
  purpose: string;
  description: string;
  risk: string;
  replaySafety: string;
  avgDurationMs: number;
  maxDurationMs: number;
  responseBytes: number;
  mimeTypes: string[];
  queryParams: QueryParam[];
  requestBody: { mimeTypes: string[]; schema: SchemaNode | null; preview: unknown };
  requestHeaderNames: string[];
  auth: { bearerObserved: boolean; cookieNames: string[]; signedRequest: boolean };
  response: { schema: SchemaNode | null; preview: unknown; businessSignals: Array<Record<string, unknown>> };
  firstSeen: string;
  lastSeen: string;
};

export type DepthLevel = {
  price: number;
  volume: number;
  orderCount: number;
};

export type OrderBookSnapshot = {
  symbol: string;
  source: string;
  timestamp: number;
  ask: DepthLevel[];
  bid: DepthLevel[];
};

export type NewsItem = {
  id: string;
  title: string;
  summary: string;
  source: string;
  publishedAt: string;
  url: string | null;
  kind: "置顶" | "实时" | "资讯";
};

export type NewsSnapshot = {
  symbol: string;
  source: string;
  items: NewsItem[];
};

export type CommunitySnapshot = {
  symbol: string;
  source: string;
  tweetCount: number;
  sampleSize: number;
  bearish: number;
  neutral: number;
  bullish: number;
  topics: Array<{ name: string; count: number }>;
};

export type Catalog = {
  summary: {
    source: string;
    capturedFrom: string;
    capturedTo: string;
    harEntries: number;
    apiCalls: number;
    sourceApiCalls: number;
    sourceEndpointCount: number;
    scope: string;
    endpointCount: number;
    usableCount: number;
    getCount: number;
    postCount: number;
    hostCount: number;
    authenticatedEndpointCount: number;
    jwtExpiryUtc: string | null;
    categoryCounts: Array<{ name: string; count: number }>;
    hostCounts: Array<{ name: string; count: number }>;
  };
  orderBooks: OrderBookSnapshot[];
  newsSnapshots: NewsSnapshot[];
  communitySnapshots: CommunitySnapshot[];
  endpoints: Endpoint[];
};
