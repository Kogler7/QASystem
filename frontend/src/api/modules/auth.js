import { request } from '../request'

export function loginApi(body) {
  return request.post('/auth/login', body)
}

export function registerApi(body) {
  return request.post('/auth/register', body)
}

export function getMeApi() {
  return request.get('/users/me')
}

export function listUsersApi() {
  return request.get('/admin/users')
}

export function createUserApi(body) {
  return request.post('/admin/users', body)
}

export function updateUserApi(id, body) {
  return request.patch(`/admin/users/${id}`, body)
}

export function deleteUserApi(id) {
  return request.delete(`/admin/users/${id}`)
}
