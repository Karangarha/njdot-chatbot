'use client'

import { useState } from 'react'
import Image from 'next/image'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { requestPasswordReset, resetPassword } from '@/lib/api'

type Stage = 'email' | 'password'

const inputCls =
  'w-full rounded-lg border border-[#E8E8E8] bg-[#F5F5F5] px-3 text-sm text-gray-800 ' +
  'placeholder:text-gray-400 transition-colors ' +
  'focus:border-[#1B3A6B] focus:bg-white focus:outline-none focus:ring-2 focus:ring-[#1B3A6B]/10 ' +
  'disabled:cursor-not-allowed disabled:opacity-50'

export default function ForgotPasswordForm() {
  const [stage, setStage]           = useState<Stage>('email')
  const [email, setEmail]           = useState('')
  const [resetToken, setResetToken] = useState('')
  const [password, setPassword]     = useState('')
  const [confirm, setConfirm]       = useState('')
  const [error, setError]           = useState<string | null>(null)
  const [isLoading, setIsLoading]   = useState(false)
  const router = useRouter()

  // ── Step 1: submit email ────────────────────────────────────────────────────
  const handleEmailSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setError(null)
    setIsLoading(true)
    try {
      const { reset_token } = await requestPasswordReset(email)
      setResetToken(reset_token)
      setStage('password')
    } catch {
      // Generic message — never reveal whether the email exists
      setError('Something went wrong. Please try again.')
    } finally {
      setIsLoading(false)
    }
  }

  // ── Step 2: set new password ────────────────────────────────────────────────
  const handlePasswordSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setError(null)

    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }

    setIsLoading(true)
    try {
      await resetPassword(resetToken, password)
      router.push('/login?reset=success')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Reset failed. Please start over.')
    } finally {
      setIsLoading(false)
    }
  }

  // ── Shared card shell ───────────────────────────────────────────────────────
  const card = (children: React.ReactNode) => (
    <div className="w-full sm:max-w-[480px]">
      <div className="rounded-2xl bg-white shadow-xl ring-1 ring-black/5 px-5 py-8 sm:p-[44px]">
        {children}
      </div>
    </div>
  )

  const header = (subtitle: string) => (
    <>
      <Link href="/" className="mb-5 block text-[13px] text-gray-400 hover:text-gray-600">
        ← Back to home
      </Link>
      <div className="mb-5 flex justify-center">
        <Image src="/njdot_logo.png" alt="NJDOT" width={64} height={64} priority />
      </div>
      <h1 className="mb-1 text-center text-xl font-bold text-[#1B3A6B]">Smart Assistant</h1>
      <p className="mb-6 text-center text-sm text-gray-400">{subtitle}</p>
    </>
  )

  // ── Stage: email ────────────────────────────────────────────────────────────
  if (stage === 'email') {
    return card(
      <>
        {header('Reset your password')}
        <form onSubmit={handleEmailSubmit} className="space-y-4">
          <div>
            <label htmlFor="email" className="mb-1.5 block text-xs font-medium text-gray-700">
              Email address
            </label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={isLoading}
              placeholder="you@example.com"
              style={{ height: '44px' }}
              className={inputCls}
            />
          </div>

          {error && (
            <div className="rounded-lg bg-red-50 px-3 py-2.5 text-xs text-red-700 ring-1 ring-red-200">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={isLoading}
            style={{ height: '44px' }}
            className="w-full rounded-lg bg-[#CC2529] text-sm font-semibold text-white transition-colors hover:bg-[#a81e21] focus:outline-none focus:ring-2 focus:ring-[#CC2529]/30 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isLoading ? 'Verifying…' : 'Continue'}
          </button>
        </form>

        <p className="mt-5 text-center text-xs text-gray-500">
          <Link href="/login" className="font-semibold text-[#1B3A6B] hover:underline">
            ← Back to sign in
          </Link>
        </p>
      </>
    )
  }

  // ── Stage: password ─────────────────────────────────────────────────────────
  return card(
    <>
      {header('Set your new password')}
      <form onSubmit={handlePasswordSubmit} className="space-y-4">
        <div>
          <label htmlFor="password" className="mb-1.5 block text-xs font-medium text-gray-700">
            New password{' '}
            <span className="font-normal text-gray-400">(min. 8 characters)</span>
          </label>
          <input
            id="password"
            type="password"
            autoComplete="new-password"
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={isLoading}
            placeholder="••••••••"
            style={{ height: '44px' }}
            className={inputCls}
          />
        </div>

        <div>
          <label htmlFor="confirm" className="mb-1.5 block text-xs font-medium text-gray-700">
            Confirm new password
          </label>
          <input
            id="confirm"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={isLoading}
            placeholder="••••••••"
            style={{ height: '44px' }}
            className={inputCls}
          />
        </div>

        {error && (
          <div className="rounded-lg bg-red-50 px-3 py-2.5 text-xs text-red-700 ring-1 ring-red-200">
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={isLoading}
          style={{ height: '44px' }}
          className="w-full rounded-lg bg-[#CC2529] text-sm font-semibold text-white transition-colors hover:bg-[#a81e21] focus:outline-none focus:ring-2 focus:ring-[#CC2529]/30 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isLoading ? 'Updating…' : 'Update Password'}
        </button>
      </form>

      <p className="mt-5 text-center text-xs text-gray-500">
        Wrong email?{' '}
        <button
          onClick={() => { setStage('email'); setError(null); setPassword(''); setConfirm('') }}
          className="font-semibold text-[#1B3A6B] hover:underline"
        >
          Start over
        </button>
      </p>
    </>
  )
}
