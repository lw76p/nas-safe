// config.js — 后端地址配置
//
// 开发阶段（默认）：在微信开发者工具里已设 urlCheck:false，可直接连局域网 NAS。
// 生产阶段：把 MODE 改成 'prod'，并填入你已备案的 HTTPS 域名。
//   更推荐：在「设置」页里手动填写后端地址，会覆盖这里的默认值并本地保存。
//
// ⚠️ 微信小程序真机/上线只认「已备案的 HTTPS 域名」。
//    局域网 http://192.168.8.62:8848 只能在开发者工具调试时用。
//    公网暴露方案见 miniprogram/README.md（Cloudflare Tunnel / 反代）。

const MODE = 'dev'; // 'dev' | 'prod'
const DEV_BASE_URL = 'http://192.168.8.62:8848';
const PROD_BASE_URL = 'https://nassafe.tsetch.com';

module.exports = {
  MODE,
  DEV_BASE_URL,
  PROD_BASE_URL,
  defaultBaseUrl: MODE === 'prod' ? PROD_BASE_URL : DEV_BASE_URL
};
