// pages/browse/browse.js — 浏览快照内文件并取回
const { get, post } = require('../../utils/request.js');

Page({
  data: {
    snapshotId: '',
    volumeId: '',
    snapName: '',
    subpath: '',
    crumbs: [],
    entries: [],
    loading: false,
    restoring: false
  },

  onLoad(opt) {
    this.setData({
      snapshotId: opt.snapshot_id || '',
      volumeId: opt.volume_id || '',
      snapName: opt.name || '快照'
    });
    wx.setNavigationBarTitle({ title: '浏览：' + (opt.name || '快照') });
    this.loadDir('');
  },

  async loadDir(subpath) {
    this.setData({ loading: true, subpath });
    const crumbs = subpath ? subpath.split('/').filter(Boolean) : [];
    try {
      const q = '/api/browse?snapshot_id=' + encodeURIComponent(this.data.snapshotId) +
                '&volume_id=' + encodeURIComponent(this.data.volumeId) +
                '&subpath=' + encodeURIComponent(subpath);
      const r = await get(q);
      const entries = (r.entries || []).slice().sort((a, b) => {
        if (a.type === b.type) return (a.name || '').localeCompare(b.name || '');
        return a.type === 'dir' ? -1 : 1;
      });
      this.setData({ entries, crumbs });
    } catch (e) {
      wx.showToast({ title: '浏览失败：' + (e.message || ''), icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  openEntry(e) {
    const idx = e.currentTarget.dataset.idx;
    const en = this.data.entries[idx];
    if (!en) return;
    if (en.type === 'dir') {
      const next = this.data.subpath ? this.data.subpath + '/' + en.name : en.name;
      this.loadDir(next);
    } else {
      this.askRestore(en.name);
    }
  },

  goCrumb(e) {
    const idx = parseInt(e.currentTarget.dataset.idx, 10);
    if (idx < 0) { this.loadDir(''); return; }
    const next = this.data.crumbs.slice(0, idx + 1).join('/');
    this.loadDir(next);
  },

  askRestore(filename) {
    wx.showModal({
      title: '取回文件',
      editable: true,
      placeholderText: 'NAS 上的目标目录，如 /share/我的文件/恢复',
      content: '',
      success: (res) => {
        if (!res.confirm) return;
        const dest = (res.content || '').trim();
        if (!dest) { wx.showToast({ title: '请填写目标目录', icon: 'none' }); return; }
        this.doRestore(filename, dest);
      }
    });
  },

  async doRestore(filename, destination) {
    this.setData({ restoring: true });
    try {
      const relative = this.data.subpath ? this.data.subpath + '/' + filename : filename;
      const r = await post('/api/snapshot/restore', {
        snapshot_id: this.data.snapshotId,
        volume_id: this.data.volumeId,
        relative_file: relative,
        destination: destination,
        confirm: true
      });
      if (r.ok) {
        wx.showModal({
          title: '取回成功',
          content: '已恢复到：\n' + (r.restored_to || destination),
          showCancel: false
        });
      } else {
        wx.showToast({ title: r.error || '取回失败', icon: 'none' });
      }
    } catch (e) {
      wx.showToast({ title: '取回失败：' + (e.message || ''), icon: 'none' });
    } finally {
      this.setData({ restoring: false });
    }
  }
});
