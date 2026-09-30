// pages/alerts/alerts.js — 告警中心（后端无 /api/alerts 时容错）
const { get } = require('../../utils/request.js');

Page({
  data: {
    alerts: [],
    enabled: true,
    loading: false
  },

  onShow() { this.load(); },

  async load() {
    this.setData({ loading: true });
    try {
      const r = await get('/api/alerts');
      if (r && r.ok) {
        const list = (r.alerts || []).map((a, i) => ({
          id: i,
          level: a.level || 'info',
          message: a.message || a.title || '',
          time: a.time || a.ts || a.created_at || ''
        }));
        this.setData({ alerts: list, enabled: true });
      } else {
        this.setData({ enabled: false, alerts: [] });
      }
    } catch (e) {
      const disabled = e.statusCode === 404 ||
        (e.raw && e.raw.error && e.raw.error.indexOf('未知接口') >= 0);
      this.setData({ enabled: !disabled, alerts: [] });
    } finally {
      this.setData({ loading: false });
    }
  }
});
