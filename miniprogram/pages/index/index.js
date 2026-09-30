// pages/index/index.js — 监控仪表盘
const { get } = require('../../utils/request.js');
const app = getApp();

Page({
  data: {
    system: null,
    version: '',
    volumes: [],
    totalSnaps: 0,
    fsText: '',
    alertCount: 0,
    alertEnabled: true,
    loading: false,
    connected: null
  },

  onShow() { this.refresh(); },

  onPullDownRefresh() {
    this.refresh().then(() => wx.stopPullDownRefresh());
  },

  async refresh() {
    if (this.data.loading) return;
    this.setData({ loading: true });
    try {
      const [sys, vol] = await Promise.all([
        get('/api/system'),
        get('/api/volumes')
      ]);
      const sysData = sys.system || {};
      const volumes = (vol.volumes || []).map(v => ({ ...v }));
      const totalSnaps = volumes.reduce((s, v) => s + (v.snapshot_count || 0), 0);
      this.setData({
        system: sysData,
        version: sys.version || '',
        volumes,
        totalSnaps,
        fsText: (sysData.fs_available || []).join(' / ') || '无',
        connected: true
      });
      this.loadAlerts();
    } catch (e) {
      this.setData({ connected: false });
      wx.showToast({ title: '连接失败：' + (e.message || '未知错误'), icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  // 告警模块容错：旧版后端无 /api/alerts，捕获 404 后标记未启用
  async loadAlerts() {
    try {
      const a = await get('/api/alerts');
      if (a && a.ok) {
        this.setData({ alertCount: (a.alerts || []).length, alertEnabled: true });
      } else {
        this.setData({ alertEnabled: false, alertCount: 0 });
      }
    } catch (e) {
      const disabled = e.statusCode === 404 ||
        (e.raw && e.raw.error && e.raw.error.indexOf('未知接口') >= 0);
      this.setData({ alertEnabled: !disabled, alertCount: 0 });
    }
  },

  openVolume(e) {
    const mp = e.currentTarget.dataset.mp;
    app.globalData.selectedVolume = mp;
    wx.switchTab({ url: '/pages/snapshots/snapshots' });
  },

  goSnapshots() { wx.switchTab({ url: '/pages/snapshots/snapshots' }); },
  goAlerts() { wx.switchTab({ url: '/pages/alerts/alerts' }); }
});
