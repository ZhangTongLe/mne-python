# Authors: Eric Larson <larson.eric.d@gmail.com>
#
# License: BSD (3-clause)

import numpy as np
import sys
from scipy.fftpack import fft, ifft
try:
    import pycuda.gpuarray as gpuarray
    from scikits.cuda import fft as cudafft
    from pycuda.driver import mem_get_info
except:
    pass

import logging
logger = logging.getLogger('mne')

from .utils import sizeof_fmt, get_config


# Support CUDA for FFTs; requires scikits.cuda and pycuda
cuda_capable = False
cuda_multiply_inplace = None
requires_cuda = np.testing.dec.skipif(True, 'CUDA not initialized')


def init_cuda():
    global cuda_capable
    global cuda_multiply_inplace
    global requires_cuda
    if get_config('MNE_USE_CUDA', 'false') == 'true':
        try:
            # Initialize CUDA; happens with importing autoinit
            import pycuda.autoinit
            assert 'pycuda.gpuarray' in sys.modules
            assert 'scikits.cuda' in sys.modules
            assert 'pycuda.driver' in sys.modules
            from pycuda.elementwise import ElementwiseKernel
            # let's construct our own CUDA multiply in-place function
            dtype = 'pycuda::complex<double>'
            cuda_multiply_inplace = \
                ElementwiseKernel(dtype + ' *a, ' + dtype + ' *b',
                                  'b[i] = a[i] * b[i]', 'multiply_inplace')
        except:
            cuda_capable = False
        else:
            cuda_capable = True
            # Figure out limit for CUDA FFT calculations
            logger.info('Enabling CUDA with %s available memory'
                        % sizeof_fmt(mem_get_info()[0]))
    else:
        cuda_capable = False
    requires_cuda = np.testing.dec.skipif(not cuda_capable,
                                          'CUDA not available')


def setup_cuda_fft_multiply_repeated(n_jobs, h_fft):
    """Set up repeated CUDA FFT multiplication with a given filter

    Parameters
    ----------
    n_jobs : int | str
        If n_jobs == 'cuda', the function will attempt to set up for CUDA
        FFT multiplication.
    h_fft : array
        The filtering function that will be used repeatedly.
        If n_jobs='cuda', this function will be shortened (since CUDA
        assumes FFTs of real signals are half the length of the signal)
        and turned into a gpuarray.

    Returns
    -------
    n_jobs : int
        Sets n_jobs = 1 if n_jobs == 'cuda' was passed in, otherwise
        original n_jobs is passed.
    cuda_dict : dict
        Dictionary with the following CUDA-related variables:
            use_cuda : bool
                Whether CUDA should be used.
            fft_plan : instance of FFTPlan
                FFT plan to use in calculating the FFT.
            ifft_plan : instance of FFTPlan
                FFT plan to use in calculating the IFFT.
            x_fft : instance of gpuarray
                Memory allocation space for storing the result of the
                frequency-domain multiplication.
            x : instance of gpuarray
                The data to filter.
    h_fft : array | instance of gpuarray
        This will either be a gpuarray (if CUDA enabled) or np.ndarray.
        If CUDA is enabled, h_fft will be modified appropriately for use
        with filter.fft_multiply().

    Notes
    -----
    This function is designed to be used with fft_multiply_repeated().
    """
    cuda_dict = dict(use_cuda=False, fft_plan=None, ifft_plan=None,
                     x_fft=None, x=None, fft_len=None)
    n_fft = len(h_fft)
    if n_jobs == 'cuda':
        n_jobs = 1
        if cuda_capable:
            # set up all arrays necessary for CUDA
            try:
                fft_plan = cudafft.Plan(n_fft, np.float64, np.complex128)
                ifft_plan = cudafft.Plan(n_fft, np.complex128, np.float64)
                if n_fft % 2 == 1:
                    cuda_fft_len = int((n_fft + 1) / 2 + 1)
                else:
                    cuda_fft_len = int(n_fft / 2 + 1)
                x_fft = gpuarray.empty(cuda_fft_len, np.complex128)
                x = gpuarray.empty(int(n_fft), np.float64)
                cuda_h_fft = h_fft[:cuda_fft_len].astype('complex128')
                # do the IFFT normalization now so we don't have to later
                cuda_h_fft /= len(h_fft)
                h_fft = gpuarray.to_gpu(cuda_h_fft)
            except:
                logger.info('CUDA not used, could not instantiate memory '
                            '(arrays may be too large), falling back to '
                            'n_jobs=1')
            else:
                logger.info('Using CUDA for FFT FIR filtering')
                cuda_dict['use_cuda'] = True
                cuda_dict['fft_plan'] = fft_plan
                cuda_dict['ifft_plan'] = ifft_plan
                cuda_dict['x_fft'] = x_fft
                cuda_dict['x'] = x
        else:
            logger.info('CUDA not used, machine is not CUDA capable, '
                        'falling back to n_jobs=1')
    return n_jobs, cuda_dict, h_fft


def fft_multiply_repeated(h_fft, x, cuda_dict=dict(use_cuda=False)):
    """Do FFT multiplication by a filter function (possibly using CUDA)

    Parameters
    ----------
    h_fft : 1-d array or gpuarray
        The filtering array to apply.
    x : 1-d array
        The array to filter.
    cuda_dict : dict
        Dictionary constructed using setup_cuda_multiply_repeated().

    Returns
    -------
    x : 1-d array
        Filtered version of x.
    """
    if not cuda_dict['use_cuda']:
        # do the fourier-domain operations
        x = np.real(ifft(h_fft * fft(x), overwrite_x=True)).ravel()
    else:
        # do the fourier-domain operations, results in second param
        cuda_dict['x'].set(x)
        cudafft.fft(cuda_dict['x'], cuda_dict['x_fft'], cuda_dict['fft_plan'])
        cuda_multiply_inplace(h_fft, cuda_dict['x_fft'])
        # If we wanted to do it locally instead of using our own kernel:
        # cuda_seg_fft.set(cuda_seg_fft.get() * h_fft)
        cudafft.ifft(cuda_dict['x_fft'], cuda_dict['x'],
                     cuda_dict['ifft_plan'], False)
        x = cuda_dict['x'].get()
    return x
