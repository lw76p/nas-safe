// app.js — NAS Safe 小程序全局逻辑
App({
  globalData: {
    baseUrl: '',        // 实际使用的后端地址（可能被设置页覆盖）
    system: null,       // /api/system 缓存
    volumes: []         // /api/volumes 缓存
  },

  onLaunch() {
    // 优先使用用户在「设置」页保存的地址
    const saved = wx.getStorageSync('nassafe_base_url');
    if (saved) {
      this.globalData.baseUrl = saved;
    }
  }
});
