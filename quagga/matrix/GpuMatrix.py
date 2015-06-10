import atexit
import ctypes
import numpy as np
from quagga.cuda import cudart, cublas, gpu_matrix_kernels


class GpuMatrix(object):
    zero_scalar = None
    one_scalar = None

    def __init__(self, data, nrows, ncols, dtype, is_owner):
        self.data = data
        self.nrows = nrows
        self.ncols = ncols
        self.nelems = nrows * ncols
        self.dtype = dtype
        self.np_dtype, self.c_dtype = self.str_to_dtypes(dtype)
        self.nbytes = self.nelems * ctypes.sizeof(self.c_dtype)
        self.is_owner = is_owner
        if is_owner:
            atexit.register(cudart.cuda_free, self.data)

    def __getitem__(self, key):
        if type(key[1]) == int:
            data = self._get_pointer_to_column(key[1])
            return GpuMatrix(data, self.nrows, 1, self.dtype, False)
        if type(key[1]) == slice:
            if key[1].start is None and type(key[1].stop) == int and key[1].step is None:
                return GpuMatrix(self.data, self.nrows, key[1].stop, self.dtype, False)
            elif type(key[1].start) == int and key[1].stop is None and key[1].step is None:
                data = self._get_pointer_to_column(key[1].start)
                return GpuMatrix(data, self.nrows, self.ncols - key[1].start, self.dtype, False)
            elif type(key[1].start) == int and type(key[1].stop) == int and key[1].step is None:
                data = self._get_pointer_to_column(key[1].start)
                return GpuMatrix(data, self.nrows, key[1].stop - key[1].start, self.dtype, False)
            else:
                raise ValueError('This slice: {} is unsupported!'.format(key))
        else:
            raise IndexError('Only integers and slices are supported!')

    def slice_columns(self, context, column_indxs, out):
        gpu_matrix_kernels.slice_columns(context.cuda_stream, self.nrows, self.ncols, column_indxs.data, self.data, out.data)

    def __del__(self):
        if self.is_owner:
            try:
                atexit._exithandlers.remove((cudart.cuda_free, (self.data, ), {}))
                cudart.cuda_free(self.data)
            except ValueError:
                pass

    def same_shape(self, other):
        return self.nrows == other.nrows and self.ncols == other.ncols

    def _get_pointer_to_column(self, k):
        void_p = ctypes.cast(self.data, ctypes.c_void_p).value + self.nrows * k * ctypes.sizeof(self.c_dtype)
        return ctypes.cast(void_p, ctypes.POINTER(self.c_dtype))

    @staticmethod
    def str_to_dtypes(dtype):
        if dtype == 'float':
            return np.float32, ctypes.c_float
        if dtype == 'int':
            return np.int32, ctypes.c_int
        raise TypeError(u'data type {} not understood'.format(dtype))

    @staticmethod
    def array_to_dtypes(a):
        if a.dtype == np.float32:
            return np.float32, ctypes.c_float
        if a.dtype == np.int32:
            return np.int32, ctypes.c_int
        raise TypeError(u'data type {} not understood'.format(a.dtype))

    @classmethod
    def from_npa(cls, a, dtype=None):
        if a.ndim != 2:
            raise ValueError('GpuMatrix works only with 2-d numpy arrays!')
        if dtype:
            np_dtype, c_dtype = cls.str_to_dtypes(dtype)
        else:
            np_dtype, c_dtype = cls.array_to_dtypes(a)
        if not np.isfortran(a):
            a = np.asfortranarray(a, dtype=np_dtype)
        elif a.dtype != np_dtype:
            a = a.astype(dtype=np_dtype)
        host_data = a.ctypes.data_as(ctypes.POINTER(c_dtype))
        elem_size = ctypes.sizeof(c_dtype)
        nbytes = a.size * elem_size
        data = cudart.cuda_malloc(nbytes, ctypes.c_float)
        cudart.cuda_memcpy(data, host_data, nbytes, 'host_to_device')
        return cls(data, a.shape[0], a.shape[1], dtype, True)

    @classmethod
    def empty(cls, nrows, ncols, dtype):
        c_dtype = cls.str_to_dtypes(dtype)[1]
        nbytes = nrows * ncols * ctypes.sizeof(c_dtype)
        data = cudart.cuda_malloc(nbytes, c_dtype)
        return cls(data, nrows, ncols, dtype, True)

    @classmethod
    def empty_like(cls, other):
        nbytes = other.nrows * other.ncols * ctypes.sizeof(other.c_dtype)
        data = cudart.cuda_malloc(nbytes, other.c_dtype)
        return cls(data, other.nrows, other.ncols, other.dtype, True)

    def to_device(self, context, a):
        if self.np_dtype != a.dtype:
            raise ValueError("Allocated memory has {} type. "
                             "Can't transfer to the device {} type".
                             format(self.np_dtype, a.dtype))
        if a.ndim != 2:
            raise ValueError('GpuMatrix works only with 2-d numpy arrays!')
        if not np.isfortran(a):
            a = np.asfortranarray(a)
        host_data = a.ctypes.data_as(ctypes.POINTER(self.c_dtype))
        self.nrows, self.ncols = a.shape
        self.nelems = self.nrows * self.ncols
        self.nbytes = self.nelems * ctypes.sizeof(self.c_dtype)
        cudart.cuda_memcpy_async(self.data, host_data, self.nbytes, 'host_to_device', context.cuda_stream)

    def to_host(self):
        c_dtype_p = ctypes.POINTER(self.c_dtype)
        host_array = (c_dtype_p * self.nelems)()
        host_ptr = ctypes.cast(host_array, c_dtype_p)
        elem_size = ctypes.sizeof(c_dtype_p)
        cudart.cuda_memcpy(host_ptr, self.data, self.nelems * elem_size, 'device_to_host')
        return np.ndarray(shape=(self.nrows, self.ncols),
                          dtype=self.np_dtype,
                          buffer=host_array,
                          order='F')

    def to_list(self):
        return [self[:, i] for i in xrange(self.ncols)]

    def hstack(self, other, out):
        """
        Stack matrices in matrix horizontally (column wise).
        """
        # TODO
        pass

    def scale(self, context, alpha, out=None):
        if out:
            gpu_matrix_kernels.scale(context.cuda_stream, self.nelems, alpha.data, self.data, out.data)
        else:
            cublas.cublas_s_scal(context.cublas_handle, self.nelems, alpha.data, self.data, 1)

    def tanh(self, context, tanh_matrix, derivative_matrix=None):
        if derivative_matrix:
            gpu_matrix_kernels.tanh_der(context.cuda_stream, self.nelems, self.data, tanh_matrix.data, derivative_matrix.data)
        else:
            gpu_matrix_kernels.tanh(context.cuda_stream, self.nelems, self.data, tanh_matrix.data)

    def sigmoid(self, context, sigmoid_matrix, derivative_matrix=None):
        if derivative_matrix:
            gpu_matrix_kernels.sigmoid_der(context.cuda_stream, self.nelems, self.data, sigmoid_matrix.data, derivative_matrix.data)
        else:
            gpu_matrix_kernels.sigmoid(context.cuda_stream, self.nelems, self.data, sigmoid_matrix.data)

    def add_scaled(self, context, alpha, a):
        """
        self += alpha * a
        """
        cublas.cublas_s_axpy(context.cublas_handle, self.nelems, alpha.data, a.data, 1, self.data, 1)

    def add(self, context, a, b=None, c=None):
        if not b and not c:
            self.add_scaled(context, GpuMatrix.one_scalar, a)
        else:
            gpu_matrix_kernels.sum(context.cuda_stream, self.nelems, a.data, b.data, c.data, self.data, self.data)

    def sliced_add(self, context, a, column_indxs, alpha=None):
        """
        self[column_indxs] += alpha * a
        """
        alpha = alpha if alpha else GpuMatrix.one_scalar
        gpu_matrix_kernels.sliced_inplace_add(context.cuda_stream, a.nrows, a.ncols, alpha.data, a.data, column_indxs, self.data)

    def add_hprod(self, context, a, b, alpha=None):
        """
        self = a .* b + alpha * self
        """
        alpha = alpha if alpha else GpuMatrix.one_scalar
        gpu_matrix_kernels.add_hadamard_product(context.cuda_stream, self.nelems, a.data, b.data, alpha.data, self.data)

    def assign_hprod(self, context, a, b, c=None):
        """
        self = a .* b
        self = a .* b .* c  or
        """
        if not c:
            gpu_matrix_kernels.hadamard_product_2(context.cuda_stream, a.nelems, a.data, b.data, self.data)
        else:
            gpu_matrix_kernels.hadamard_product_3(context.cuda_stream, a.nelems, a.data, b.data, c.data, self.data)

    def assign_sum_hprod(self, context, a, b, c, d, e=None, f=None, g=None, h=None, i=None, j=None, k=None):
        """
        self = a .* b + c .* d                                   or
        self = a .* b .* c + d .* e                              or
        self = a .* b .* c + d .* e + f .* g + h .* i + j .* k
        """
        if k is not None:
            gpu_matrix_kernels.sum_hprod_11(context.cuda_stream, self.nelems, a.data, b.data, c.data, d.data, e.data, f.data, g.data, h.data, i.data, j.data, k.data, self.data)
        elif e is not None:
            gpu_matrix_kernels.sum_hprod_5(context.cuda_stream, self.nelems, a.data, b.data, c.data, d.data, e.data, self.data)
        else:
            gpu_matrix_kernels.sum_hprod_4(context.cuda_stream, self.nelems, a.data, b.data, c.data, d.data, self.data)

    def assign_hprod_sum(self, context, a, b):
        """
        self = sum(a .* b, axis=1)
        """
        gpu_matrix_kernels.hprod_sum(context.cuda_stream, a.nrows, a.ncols, a.data, b.data, self.data)

    def assign_dot(self, context, a, b, matrix_operation_a='N', matrix_operation_b='N'):
        self.add_dot(context, a, b, matrix_operation_a, matrix_operation_b, beta=GpuMatrix.zero_scalar)

    def add_dot(self, context, a, b, matrix_operation_a='N', matrix_operation_b='N', alpha=None, beta=None):
        """
        self = alpha * op(a) * b + beta * self
        """
        alpha = alpha if alpha else GpuMatrix.one_scalar
        beta = beta if beta else GpuMatrix.one_scalar

        if self.ncols == 1 and matrix_operation_b == 'N':
            cublas.cublas_s_gemv(context.cublas_handle, matrix_operation_a, a.nrows, a.ncols, alpha.data, a.data, a.nrows, b.data, 1, beta.data, self.data, 1)
        else:
            k = b.nrows if matrix_operation_b == 'N' else b.ncols
            cublas.cublas_s_gemm(context.cublas_handle, matrix_operation_a, matrix_operation_b, self.nrows, self.ncols, k, alpha.data, a.data, a.nrows, b.data, b.nrows, beta.data, self.data, self.nrows)

    def vdot(self, context, a, result=None):
        result = result if result else GpuMatrix.empty(1, 1, 'float')
        cublas.cublas_s_dot(context.cublas_handle, self.nelems, self.data, 1, a.data, 1, result.data)
        return result


GpuMatrix.zero_scalar = GpuMatrix.from_npa(np.zeros((1, 1)), 'float')
GpuMatrix.one_scalar = GpuMatrix.from_npa(np.ones((1, 1)), 'float')