import { apiRequest } from './http'

export type AccessTokenType = 'cli' | 'sync'
export type AccessTokenLevel = 'read_only' | 'read_write' | 'sync'

export interface AccessTokenSummary {
  id: string
  token_type: AccessTokenType
  access_level: AccessTokenLevel
  name: string
  prefix: string
  scopes: string[]
  is_active: boolean
  created_at: string
  last_used_at: string | null
}

interface AccessTokenListResponse {
  ok: true
  tokens: AccessTokenSummary[]
}

interface AccessTokenIssueResponse {
  ok: true
  token: AccessTokenSummary
  secret: string
  secret_display: 'once'
}

interface AccessTokenRevokeResponse {
  ok: true
  id: string
  token_type: AccessTokenType
  is_active: false
}

interface AccessTokenDeleteResponse {
  ok: true
  id: string
  token_type: AccessTokenType
  deleted: true
}

export const tokensApi = {
  async list(): Promise<AccessTokenSummary[]> {
    const payload = await apiRequest<AccessTokenListResponse>('/api/accounts/v1/tokens/')
    return payload.tokens
  },

  issue(name: string, accessLevel: AccessTokenLevel) {
    const tokenType: AccessTokenType = accessLevel === 'sync' ? 'sync' : 'cli'
    return apiRequest<AccessTokenIssueResponse>('/api/accounts/v1/tokens/', {
      method: 'POST',
      json: {
        name,
        token_type: tokenType,
        ...(tokenType === 'cli' ? { access_level: accessLevel } : {}),
      },
    })
  },

  revoke(token: Pick<AccessTokenSummary, 'id' | 'token_type'>) {
    return apiRequest<AccessTokenRevokeResponse>(`/api/accounts/v1/tokens/${token.token_type}/${encodeURIComponent(token.id)}/`, {
      method: 'DELETE',
    })
  },

  remove(token: Pick<AccessTokenSummary, 'id' | 'token_type'>) {
    return apiRequest<AccessTokenDeleteResponse>(`/api/accounts/v1/tokens/${token.token_type}/${encodeURIComponent(token.id)}/delete/`, {
      method: 'DELETE',
    })
  },
}
