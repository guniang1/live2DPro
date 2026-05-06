import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default {
  resolve: {
    alias: {
      '@framework': path.resolve(__dirname, './../Framework/src')
    }
  },
  server: {
    // 避免 localhost 域名下堆积 Cookie 导致 431 请求头过大
    host: '127.0.0.1',
    hmr: {
      host: '127.0.0.1'
    }
  }
};
