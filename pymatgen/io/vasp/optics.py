# Copyright (c) Pymatgen Development Team.
# Distributed under the terms of the MIT License.
"""Classes for parsing and manipulating VASP optical properties calculations."""

import itertools

import numpy as np
import numpy.typing as npt
from scipy import constants, special

# from pymatgen.core import Structure
# from pymatgen.io.vasp.outputs import Waveder

au2ang = constants.physical_constants["atomic unit of length"][0] / 1e-10
ryd2ev = constants.physical_constants["Rydberg constant times hc in eV"][0]
edeps = 4 * np.pi * 2 * ryd2ev * au2ang  # from constant.inc in VASP

KB = constants.physical_constants["Boltzmann constant in eV/K"][0]


def get_eps_constant(structure):
    return edeps * np.pi / structure.volume


def delta_methfessel_paxton(x, n):
    """
    D_n (x) = exp -x^2 * sum_i=0^n A_i H_2i(x)
    where H is a Hermite polynomial and
    A_i = (-1)^i / ( i! 4^i sqrt(pi) )
    """
    ii = np.arange(0, n + 1)
    A = (-1) ** ii / (special.factorial(ii) * 4**ii * np.sqrt(np.pi))
    H = special.eval_hermite(ii * 2, np.tile(x, (len(ii), 1)).T)
    return np.exp(-(x * x)) * np.dot(A, H.T)


def step_methfessel_paxton(x, n):
    """
    S_n (x) = (1 + erf x)/2 - exp -x^2 * sum_i=1^n A_i H_{2i-1}(x)
    where H is a Hermite polynomial and
    A_i = (-1)^i / ( i! 4^i sqrt(pi) )
    """
    ii = np.arange(1, n + 1)
    A = (-1) ** ii / (special.factorial(ii) * 4**ii * np.sqrt(np.pi))
    H = special.eval_hermite(ii * 2 - 1, np.tile(x, (len(ii), 1)).T)
    return (1.0 + special.erf(x)) / 2.0 - np.exp(-(x * x)) * np.dot(A, H.T)


def delta_func(x, ismear):
    """Replication of VASP's delta function"""
    if ismear == -1:
        return step_func(x, -1) * (1 - step_func(x, -1))
    elif ismear < 0:
        return np.exp(-(x * x)) / np.sqrt(np.pi)
    return delta_methfessel_paxton(x, ismear)


def step_func(x, ismear):
    """Replication of VASP's step function"""
    if ismear == -1:
        return 1 / (1.0 + np.exp(-x))
    elif ismear < 0:
        return 0.5 + 0.5 * special.erf(x)
    return step_methfessel_paxton(x, ismear)


def get_delta(x0: float, sigma: float, nx: int, dx: float, ismear: int = 3):
    """Get the smeared delta function to be added to form the spectrum.

    This replaces the `SLOT` function from VASP.

    Args:
        x0: The center of the dielectric function.
        sigma: The width of the smearing
        nx: The number of grid points in the output grid.
        dx: The gridspacing of the output grid.
        ismear: The smearing parameter used by the ``step_func``.

    Return:
        np.array: Array of size `nx` with delta function on the desired outputgrid.

    """
    xgrid = np.arange(0, nx * dx, dx)
    xgrid -= x0
    x_scaled = (xgrid + (dx / 2)) / sigma
    sfun = step_func(x_scaled, ismear)
    dfun = np.zeros_like(xgrid)
    dfun[1:] = (sfun[1:] - sfun[:-1]) / dx
    return dfun


def get_step(x0, sigma, nx, dx, ismear):
    """Get the smeared step function to be added to form the spectrum.

    This replaces the `SLOT` function from VASP.

    Args:
        x0: The center of the dielectric function.
        sigma: The width of the smearing
        nx: The number of grid points in the output grid.
        dx: The gridspacing of the output grid.
        ismear: The smearing parameter used by the ``step_func``.

    Return:
        np.array: Array of size `nx` with step function on the desired outputgrid.

    """
    xgrid = np.arange(0, nx * dx, dx)
    xgrid -= x0
    x_scaled = (xgrid + (dx / 2)) / sigma
    return step_func(x_scaled, ismear)


def epsilon_imag(
    cder: npt.NDArray,
    eigs: npt.NDArray,
    kweights: npt.ArrayLike,
    efermi: float,
    nedos: int,
    deltae: float,
    ismear: int,
    sigma: float,
):
    """Replicate the EPSILON_IMAG function of VASP.

    Args:
        cder: The data written to the WAVEDER (nbands, nbands, nkpoints, nspin, diri, dirj)
        eigs: The eigenvalues (nbands, nkpoints, nspin)
        kweights: The kpoint weights (nkpoints)
        efermi: The fermi energy
        nedos: The sampling of the energy values
        deltae: The energy grid spacing
        ismear: The smearing parameter used by the ``step_func``.
        sigma: The width of the smearing

    Return:
        np.array: Array of size `nedos` with the imaginary part of the dielectric function.

    """
    norm_kweights = np.array(kweights) / np.sum(kweights)
    egrid = np.arange(0, nedos * deltae, deltae)
    eigs_shifted = eigs - efermi
    # np.subtract.outer results in a matrix of shape (nband, nband)
    rspin = 3 - cder.shape[3]

    # for the transition between two bands at one kpoint the contributions is:
    #  (fermi[band_i] - fermi[band_j]) * rspin * normalized_kpoint_weight

    for idir, jdir in itertools.product(range(3), range(3)):
        epsdd = np.zeros_like(egrid, dtype=np.complex128)
        for ib, jb, ik, ispin in np.ndindex(cder.shape[:4]):
            # print(f"ib={ib}, jb={jb}, ik={ik}, ispin={ispin}")
            fermi_w_i = step_func((eigs_shifted[ib, ik]) / sigma, ismear)
            fermi_w_j = step_func((eigs_shifted[jb, ik]) / sigma, ismear)
            weight = (fermi_w_j - fermi_w_i) * rspin * norm_kweights[ik]
            decel = eigs[jb, ik] - eigs[ib, ik]
            A = cder[ib, jb, ik, ispin, idir] * np.conjugate(cder[ib, jb, ik, ispin, jdir])
            # Reproduce the `SLOT` function calls in VASP:
            # CALL SLOT( REAL(DECEL,q), ISMEAR, SIGMA, NEDOS, DELTAE,  WEIGHT*A*CONST, EPSDD)
            # The conjugate part is not needed since we are running over all pairs of ib, jb
            # vasp just does the conjugate trick to save loop time
            smeared = get_delta(x0=decel, sigma=sigma, nx=nedos, dx=deltae, ismear=ismear) * weight * A
            epsdd += smeared
        yield egrid, epsdd


# def epsilon_real():
#     """Perform Kramer-Kronig transformation to get the real part of the dielectric function.

#     > EPSILON_REAL( WDES%COMM, NEDOS, EPSDD(:,IDIR,JDIR), LWARN, DELTAE, LCSHIFT, IWINDOW, WPLASMA_INTER(IDIR, JDIR))
#     > WDES%COMM: for VASP MPI
#     > LWARN, IWINDOW, WPLASMA_INTER: not used in this function
#     > NEDOS: number of points in the energy grid
#     > EPSDD: dielectric function after EPSILON_IMAG is called
#     > DELTAE: energy grid spacing
#     > LCSHIFT: Complex shift of poles in the dielectric function
#     """
