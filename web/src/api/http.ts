import type { ApiErrorResponse } from './types'

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE'])
let csrfBootstrap: Promise<void> | null = null

export interface ApiRequestOptions extends Omit<RequestInit, 'body'> {
  body?: BodyInit | null
  json?: unknown
}

export class ApiClientError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown
  readonly retryAfterSeconds: number | null

  constructor(status: number, code: string, message: string, details: unknown = {}, retryAfterSeconds: number | null = null) {
    super(message)
    this.name = 'ApiClientError'
    this.status = status
    this.code = code
    this.details = details
    this.retryAfterSeconds = retryAfterSeconds
  }

  get unauthorized() {
    return this.status === 401 || this.code === 'AUTHENTICATION_REQUIRED'
  }
}

export function getCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const value = document.cookie
    .split(';')
    .map((cookie) => cookie.trim())
    .find((cookie) => cookie.startsWith(prefix))
    ?.slice(prefix.length)
  return value ? decodeURIComponent(value) : null
}

function parsePayload(text: string): unknown {
  if (!text) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

function legacyApiError(status: number, statusText: string, payload: unknown): ApiClientError {
  const common = payload as Partial<ApiErrorResponse> | undefined
  if (common?.error && typeof common.error === 'object') {
    return new ApiClientError(
      status,
      String(common.error.code || 'API_ERROR'),
      String(common.error.message || statusText || 'API request failed.'),
      common.error.details,
    )
  }
  const legacy = payload as { errors?: unknown; detail?: unknown; code?: unknown } | undefined
  return new ApiClientError(
    status,
    typeof legacy?.code === 'string' ? legacy.code : (status === 401 ? 'AUTHENTICATION_REQUIRED' : 'API_ERROR'),
    typeof legacy?.detail === 'string' ? legacy.detail : statusText || 'API request failed.',
    legacy?.errors ?? payload ?? {},
  )
}

async function readResponse(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('json')) {
    try {
      return await response.json()
    } catch {
      return undefined
    }
  }
  const text = await response.text()
  return text || undefined
}

function toApiError(response: Response, payload: unknown): ApiClientError {
  return legacyApiError(response.status, response.statusText, payload)
}

export async function apiError(response: Response): Promise<ApiClientError> {
  const payload = await readResponse(response)
  const error = toApiError(response, payload)
  const retryAfter = Number.parseInt(response.headers.get('Retry-After') ?? '', 10)
  return new ApiClientError(
    error.status,
    error.code,
    error.message,
    error.details,
    Number.isFinite(retryAfter) ? retryAfter : null,
  )
}

async function ensureCsrfCookie(): Promise<void> {
  if (getCookie('csrftoken')) return
  csrfBootstrap ??= fetch('/api/accounts/v1/session/', {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  }).then(async (response) => {
    const payload = await readResponse(response)
    if (!response.ok) throw toApiError(response, payload)
  }).finally(() => {
    csrfBootstrap = null
  })
  await csrfBootstrap
}

export async function apiFetch(path: string, options: ApiRequestOptions = {}): Promise<Response> {
  const { json, ...requestOptions } = options
  const method = (requestOptions.method ?? 'GET').toUpperCase()
  const headers = new Headers(requestOptions.headers)
  headers.set('Accept', 'application/json')

  let body = requestOptions.body
  if (json !== undefined) {
    headers.set('Content-Type', 'application/json')
    body = JSON.stringify(json)
  }

  if (!SAFE_METHODS.has(method)) {
    await ensureCsrfCookie()
    const csrfToken = getCookie('csrftoken')
    if (csrfToken) headers.set('X-CSRFToken', csrfToken)
  }

  return fetch(path, {
    ...requestOptions,
    method,
    headers,
    body,
    credentials: 'same-origin',
  })
}

export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const response = await apiFetch(path, options)
  if (!response.ok) throw await apiError(response)
  const payload = await readResponse(response)
  return payload as T
}

export function uploadForm<T>(path: string, formData: FormData, onProgress?: (percent: number) => void): Promise<T> {
  return ensureCsrfCookie().then(() => new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    request.open('POST', path)
    request.responseType = 'text'
    request.withCredentials = true
    request.setRequestHeader('Accept', 'application/json')
    const csrfToken = getCookie('csrftoken')
    if (csrfToken) request.setRequestHeader('X-CSRFToken', csrfToken)
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100))
    })
    request.addEventListener('load', () => {
      const payload = parsePayload(request.responseText)
      if (request.status >= 200 && request.status < 300) {
        onProgress?.(100)
        resolve(payload as T)
      } else {
        reject(legacyApiError(request.status, request.statusText, payload))
      }
    })
    request.addEventListener('error', () => reject(new ApiClientError(0, 'NETWORK_ERROR', 'Network request failed.')))
    request.addEventListener('abort', () => reject(new ApiClientError(0, 'REQUEST_ABORTED', 'Upload was canceled.')))
    request.send(formData)
  }))
}
