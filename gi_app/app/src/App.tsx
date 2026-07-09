import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { Toaster } from 'sonner'
import Layout from '@/components/Layout'
import Overview from '@/pages/Overview'
import DatasetDetail from '@/pages/DatasetDetail'
import AddData from '@/pages/AddData'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Overview />} />
          <Route path="/datasets/:datasetId" element={<DatasetDetail />} />
          <Route path="/add-data" element={<AddData />} />
        </Route>
      </Routes>
      <Toaster richColors position="top-right" />
    </BrowserRouter>
  )
}

export default App
