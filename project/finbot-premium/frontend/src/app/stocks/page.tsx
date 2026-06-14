import FeedPage from '@/components/FeedPage';
import { Category } from '@/types';

export default function Page() {
  return <FeedPage category={'stocks' as Category} />;
}
