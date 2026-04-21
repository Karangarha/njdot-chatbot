import type { Metadata } from 'next'
import LoginForm from '@/components/auth/LoginForm'

export const metadata: Metadata = {
  title: 'Sign In — NJDOT Spec Assistant',
}

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ reset?: string }>
}) {
  const { reset } = await searchParams
  return (
    <main className="flex min-h-screen items-center justify-center bg-[#F5F5F5] px-4 py-12">
      <LoginForm resetSuccess={reset === 'success'} />
    </main>
  )
}
