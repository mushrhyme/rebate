import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Layout } from './components/Layout'
import { ProtectedRoute } from './components/ProtectedRoute'
import { ChangePasswordModal } from './components/ChangePasswordModal'
import { Dashboard } from './pages/Dashboard'
import { Upload } from './pages/Upload'
import { MappingReview } from './pages/MappingReview'
import { Results } from './pages/Results'
import { FormManagement } from './pages/FormManagement'
import { ColdStart } from './pages/ColdStart'
import { Sap } from './pages/Sap'
import { UserManagement } from './pages/UserManagement'
import { RetailAssignment } from './pages/RetailAssignment'
import { UsageMonitor } from './pages/UsageMonitor'
import { DslStudio } from './pages/DslStudio'
import { Login } from './pages/Login'
import { AuthProvider, useAuth } from './context/AuthContext'
import { FormsProvider } from './context/FormsContext'

function AdminRoute({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  if (!user) return <Navigate to="/login" replace />
  if (!user.is_admin && user.username !== 'admin') return <Navigate to="/" replace />
  return <>{children}</>
}

function ProtectedLayout({ children }: { children: React.ReactNode }) {
  return (
    <ProtectedRoute>
      <Layout>{children}</Layout>
    </ProtectedRoute>
  )
}

function AppRoutes() {
  const { mustChangePassword } = useAuth()

  if (mustChangePassword) return <ChangePasswordModal />

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<ProtectedLayout><Dashboard /></ProtectedLayout>} />
      <Route path="/upload" element={<ProtectedLayout><Upload /></ProtectedLayout>} />
      <Route path="/mapping/:id" element={<ProtectedLayout><MappingReview /></ProtectedLayout>} />
      <Route path="/results/:id" element={<ProtectedLayout><Results /></ProtectedLayout>} />
      <Route path="/forms" element={<ProtectedLayout><FormManagement /></ProtectedLayout>} />
      <Route path="/cold-start" element={<ProtectedLayout><ColdStart /></ProtectedLayout>} />
      <Route path="/sap" element={<ProtectedLayout><Sap /></ProtectedLayout>} />
      <Route path="/admin/users" element={
        <ProtectedRoute>
          <AdminRoute>
            <Layout><UserManagement /></Layout>
          </AdminRoute>
        </ProtectedRoute>
      } />
      <Route path="/admin/retail-assignment" element={
        <ProtectedRoute>
          <AdminRoute>
            <Layout><RetailAssignment /></Layout>
          </AdminRoute>
        </ProtectedRoute>
      } />
      <Route path="/admin/usage" element={
        <ProtectedRoute>
          <AdminRoute>
            <Layout><UsageMonitor /></Layout>
          </AdminRoute>
        </ProtectedRoute>
      } />
      <Route path="/dsl-studio" element={
        <ProtectedRoute>
          <AdminRoute>
            <Layout><DslStudio /></Layout>
          </AdminRoute>
        </ProtectedRoute>
      } />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <FormsProvider>
          <AppRoutes />
        </FormsProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
