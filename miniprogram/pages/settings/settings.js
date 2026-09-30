// pages/settings/settings.js — 后端地址与模块设置
const { get } = require('../../utils/request.js');
const config = require('../../config.js');
const app = getApp();

Page({
  data: {
    baseUrl: '',
    savedUrl: '',
    testing: false,
    notifyEnabled: null,
    aiEnabled: null
  },

  onLoad() {
    const saved = wx.getStorageSync('nassafe_base_url') || config.defaultBaseUrl;
    this.setData({ baseUrl: saved, savedUrl: saved });
  },

  onShow() { this.probeModules(); },

  onInput(e) { this.setData({ baseUrl: e.detail.value }); },

  save() {
    let url = (this.data.baseUrl || '').trim().replace(/\/+$/, '');
    if (!url) { wx.showToast({ title: '地址不能为空', icon: 'none' }); return; }
    wx.setStorageSync('nassafe_base_url', url);
    app.globalData.baseUrl = url;
    this.setData({ savedUrl: url });
    wx.showToast({ title: '已保存', icon: 'success' });
    this.probeModules();
  },

  async test() {
    if (!this.data.baseUrl) return;
    this.setData({ testing: true });
    try {
      const r = await get('/api/health');
      if (r && r.ok) {
        wx.showToast({ title: '连接成功', icon: 'success' });
      } else {
        wx.showToast({ title: '后端返回异常', icon: 'none' });
      }
    } catch (e) {
      wx.showModal({ title: '连接失败', content: e.message || '请检查地址与网络', showCancel: false });
    } finally {
      this.setData({ testing: false });
    }
  },

  async probeModules() {
    const isDisabled = (e) => e.statusCode === 404 ||
      (e.raw && e.raw.error && e.raw.error.indexOf('未知接口') >= 0);
    try {
      const r = await get('/api/notify/config');
      this.setData({ notifyEnabled: !!(r && r.ok) });
    } catch (e) {
      this.setData({ notifyEnabled: isDisabled(e) ? false : null });
    }
    try {
      const r = await get('/api/ai/config');
      this.setData({ aiEnabled: !!(r && r.ok) });
    } catch (e) {
      this.setData({ aiEnabled: isDisabled(e) ? false : null });
    }
  },

  openReadme() {
    wx.showModal({
      title: '真机访问说明',
      content: '微信小程序真机/上线只认「已备案的 HTTPS 域名」。\n\n开发调试：在微信开发者工具勾选「不校验合法域名」即可直连局域网地址。\n\n生产方案：把 NAS Safe 后端经 Cloudflare Tunnel / 反代暴露为已备案 HTTPS 域名，详见 miniprogram/README.md。',
      showCancel: false
    });
  }
});
