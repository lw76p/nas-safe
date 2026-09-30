// pages/snapshots/snapshots.js — 快照管理
const { get, post } = require('../../utils/request.js');
const app = getApp();

Page({
  data: {
    volumes: [],
    volIndex: 0,
    currentVolume: '',
    currentVolumeId: '',
    currentVolumeName: '',
    snapshots: [],
    loading: false,
    creating: false
  },

  async onShow() { await this.loadVolumes(); },

  async loadVolumes() {
    this.setData({ loading: true });
    try {
      const vol = await get('/api/volumes');
      const volumes = vol.volumes || [];
      const preferred = app.globalData.selectedVolume;
      let idx = 0;
      if (preferred) {
        const found = volumes.findIndex(v => v.mountpoint === preferred || v.name === preferred);
        if (found >= 0) idx = found;
      }
      const sel = volumes[idx];
      if (sel) {
        this.setData({
          volumes,
          volIndex: idx,
          currentVolume: sel.mountpoint,
          currentVolumeId: sel.volume_id || sel.mountpoint,
          currentVolumeName: sel.name
        });
        app.globalData.selectedVolume = sel.mountpoint;
        await this.loadSnapshots();
      } else {
        this.setData({ volumes });
      }
    } catch (e) {
      wx.showToast({ title: '加载失败：' + (e.message || ''), icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  onVolumePick(e) {
    const idx = parseInt(e.detail.value, 10) || 0;
    const sel = this.data.volumes[idx];
    if (!sel) return;
    this.setData({
      volIndex: idx,
      currentVolume: sel.mountpoint,
      currentVolumeId: sel.volume_id || sel.mountpoint,
      currentVolumeName: sel.name
    });
    app.globalData.selectedVolume = sel.mountpoint;
    this.loadSnapshots();
  },

  async loadSnapshots() {
    if (!this.data.currentVolume) return;
    this.setData({ loading: true });
    try {
      const r = await get('/api/snapshots?volume=' + encodeURIComponent(this.data.currentVolume));
      this.setData({ snapshots: r.snapshots || [] });
    } catch (e) {
      wx.showToast({ title: '加载快照失败：' + (e.message || ''), icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  async createSnapshot() {
    if (!this.data.currentVolume) return;
    this.setData({ creating: true });
    try {
      const r = await post('/api/snapshot/create', { volume: this.data.currentVolume });
      if (r.ok) {
        wx.showToast({ title: '快照已创建', icon: 'success' });
        await this.loadSnapshots();
      } else {
        wx.showToast({ title: r.error || '创建失败', icon: 'none' });
      }
    } catch (e) {
      wx.showToast({ title: '创建失败：' + (e.message || ''), icon: 'none' });
    } finally {
      this.setData({ creating: false });
    }
  },

  openSnapshot(e) {
    const idx = e.currentTarget.dataset.idx;
    const s = this.data.snapshots[idx];
    if (!s) return;
    if (!s.snapshot_id) {
      wx.showToast({ title: '该快照需本地浏览（移动端暂不支持）', icon: 'none' });
      return;
    }
    wx.navigateTo({
      url: '/pages/browse/browse?snapshot_id=' + encodeURIComponent(s.snapshot_id) +
           '&volume_id=' + encodeURIComponent(this.data.currentVolumeId) +
           '&name=' + encodeURIComponent(s.name)
    });
  }
});
