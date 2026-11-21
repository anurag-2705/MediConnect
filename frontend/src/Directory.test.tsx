import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Explore } from './Directory'
import type { Doctor } from './api'

const doctor = (id: number) => ({ id, full_name: `Dr Number${id}`, specialty: 'General Medicine', city: 'Kolkata', experience_years: id,
  clinic_fee_rupees: 300, online_fee_rupees: 400, rating: null, review_count: 0, matching_modes: ['clinic', 'online'] }) as unknown as Doctor

let urls: string[] = []
function mockDoctors(total: number) {
  urls = []
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    urls.push(String(input))
    const limit = Number(new URL(String(input), 'http://x').searchParams.get('limit')) || total
    return new Response(JSON.stringify(Array.from({ length: Math.min(limit, total) }, (_, index) => doctor(index + 1))),
      { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
}
function Where() { const { search } = useLocation(); return <output data-testid="search">{search}</output> }
const view = (initial = '/doctors?city=Kolkata') => render(<MemoryRouter initialEntries={[initial]}><Explore /><Where /></MemoryRouter>)
afterEach(() => vi.unstubAllGlobals())

describe('doctor directory', () => {
  it('shows a page of results, tells the user there are more, and loads the next page on request', async () => {
    mockDoctors(30)
    const user = userEvent.setup()
    view()
    expect(await screen.findByRole('heading', { name: '12+ doctors found' })).toBeInTheDocument()
    expect(screen.getAllByRole('article')).toHaveLength(12)
    expect(urls.at(-1)).toContain('limit=13')
    await user.click(screen.getByRole('button', { name: 'Show more doctors' }))
    expect(await screen.findByRole('heading', { name: '24+ doctors found' })).toBeInTheDocument()
    expect(urls.at(-1)).toContain('limit=25')
    expect(screen.getByTestId('search').textContent).toContain('pages=2')
  })

  it('hides "Show more" and gives an exact count when everything fits on one page', async () => {
    mockDoctors(5)
    view()
    expect(await screen.findByRole('heading', { name: '5 doctors found' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show more doctors' })).not.toBeInTheDocument()
  })

  it('sorts through the server, keeps the other filters, and starts again from the first page', async () => {
    mockDoctors(30)
    const user = userEvent.setup()
    view('/doctors?city=Kolkata&pages=2')
    await screen.findByRole('heading', { name: '24+ doctors found' })
    await user.selectOptions(screen.getByLabelText('Sort by'), 'fee_low')
    await waitFor(() => expect(urls.at(-1)).toContain('sort=fee_low'))
    expect(urls.at(-1)).toContain('city=Kolkata')
    expect(urls.at(-1)).toContain('limit=13')
    expect(screen.getByTestId('search').textContent).not.toContain('pages=')
    await user.selectOptions(screen.getByLabelText('Sort by'), 'relevance')
    await waitFor(() => expect(screen.getByTestId('search').textContent).not.toContain('sort='))
  })

  it('keeps the chosen order when filters are applied', async () => {
    mockDoctors(3)
    const user = userEvent.setup()
    view('/doctors?sort=rating')
    await screen.findByRole('heading', { name: '3 doctors found' })
    await user.type(screen.getByLabelText('City'), 'Pune')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await waitFor(() => expect(screen.getByTestId('search').textContent).toContain('city=Pune'))
    expect(screen.getByTestId('search').textContent).toContain('sort=rating')
  })
})
