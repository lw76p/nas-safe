// utils/request.js — wx.request 封装
// 统一处理：baseUrl 来源（设置页优先）、JSON 序列化、错误归一化。
const config = require('../config.js');

function getBaseUrl() {
  const saved = wx.getStorageSync('nassafe_base_url');
  if (saved) return saved;
  return config.defaultBaseUrl;
}

/**
 * 发起请求
 * @param {string} path 形如 '/api/volumes'
 * @param {string} method 'GET' | 'POST'
 * @param {object} data 请求体 / 查询参数
 * @returns {Promise<object>} 解析后的 JSON；非 2xx 或网络错误 reject
 */
function request(path, method, data) {
  return new Promise((resolve, reject) => {
    const isGet = (method || 'GET').toUpperCase() === 'GET';
    wx.request({
      url: getBaseUrl() + path,
      method: method || 'GET',
      data: data || {},
      header: { 'Content-Type': 'application/json' },
      timeout: 15000,
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data);
        } else {
          // 归一化错误：后端约定返回 {ok:false, error:"..."}
          const err = (res.data && res.data.error) ? res.data.error : ('HTTP ' + res.statusCode);
          reject({ statusCode: res.statusCode, message: err, raw: res.data });
        }
      },
      fail(err) {
        reject({ statusCode: 0, message: err.errMsg || '网络请求失败', raw: err });
      }
    });
  });
}

// GET 便捷方法
function get(path, data) { return request(path, 'GET', data); }
// POST 便捷方法
function post(path, data) { return request(path, 'POST', data); }

module.exports = { request, get, post, getBaseUrl };
