import meds
import ngmix
import numpy as np
import pdb, pudb
import pickle
from astropy.table import Table, Row, vstack, hstack
import os, sys, time, traceback
import galsim
import matplotlib.pyplot as plt
from argparse import ArgumentParser
import matplotlib.pyplot as plt
import matplotlib.colors as colors
from mpl_toolkits.axes_grid1 import make_axes_locatable
from numba.core.errors import NumbaExperimentalFeatureWarning, NumbaDeprecationWarning
import warnings
import time

from multiprocessing import Pool
import multiprocessing

import superbit_lensing.utils as sb_utils

#sing.log_to_stderr()
# mpl.setLevel(logging.INFO)

parser = ArgumentParser()

parser.add_argument('medsfile', type=str,
                    help='MEDS file to process')
parser.add_argument('outfile', type=str,
                    help='Output filename')
parser.add_argument('--start', type=int, default=None,
                    help='Starting index for MEDS processing')
parser.add_argument('--end', type=int, default=None,
                    help='Ending index for MEDS processing')
parser.add_argument('--plot', type=str, default=True,
                    help='Make diagnoistic plots')
parser.add_argument('--n', type=int, default=None,
                    help='Number of cores to use')
parser.add_argument('-v', '--verbose', action='store_true',
                    help='Make verbose')

class ForkedPdb(pdb.Pdb):
    """A Pdb subclass that may be used
    from a forked multiprocessing child
    https://stackoverflow.com/questions/4716533/how-to-attach-debugger-to-a-python-subproccess
    """
    def interaction(self, *args, **kwargs):
        _stdin = sys.stdin
        try:
            sys.stdin = open('/dev/stdin')
            pdb.Pdb.interaction(self, *args, **kwargs)
        finally:
            sys.stdin = _stdin

        return

class SuperBITNgmixFitter():
    """
    class to process a set of observations from a MEDS file.

    meds_info: A dict or list of dicts telling us how to process the one or meds
               files provided.

    """
    def __init__(self, config, meds_info=None):

        # The meds file groups data together by source.
        # To run, we need to make an ObservationList object for each source.
        # Each ObservationList has a single Observation frome each epoch.
        # Each Observation object needs an image, a psf, a weightmap, and a wcs, expressed as a Jacobian object.
        #   The MEDS object has methods to get all of these.

        # Once that's done, we create a Bootstrapper, populate it with initial guesses
        #   and priors, and run it.
        # The results get compiled into a catalog (numpy structured array with named fields),
        #   which is either written to a .fits table or returned in memory.
        #
        # self.metcal will hold result of metacalibration
        # self.gal_results is the result of a simple fit to the galaxy shape within metcal bootstrapper

        self.config = config
        self.seed = config['seed']
        self.medsObj = meds.MEDS(meds_info['meds_file'])
        self.catalog = self.medsObj.get_cat()

        try:
            self.verbose = config['verbose']
        except KeyError:
            self.verbose = False

        return

    def _generate_initial_guess(self,observation):
        # Generate a guess from the pixel scale.
        fwhm_guess= 4*observation.jacobian.get_scale()
        gmom = ngmix.gaussmom.GaussMom(observation,fwhm_guess)
        gmom.go()

        return gmom.result

    def _get_priors(self):

        # This bit is needed for ngmix v2.x.x
        # won't work for v1.x.x
        #rng = np.random.RandomState(self.seed)

        # prior on ellipticity.  The details don't matter, as long
        # as it regularizes the fit.  This one is from Bernstein & Armstrong 2014

        g_sigma = 0.3
        g_prior = ngmix.priors.GPriorBA(g_sigma)

        # 2-d gaussian prior on the center
        # row and column center (relative to the center of the jacobian, which would be zero)
        # and the sigma of the gaussians

        # units same as jacobian, probably arcsec
        row, col = 0.0, 0.0
        row_sigma, col_sigma = 0.2, 0.2 # a bit smaller than pix size of SuperBIT
        cen_prior = ngmix.priors.CenPrior(row, col, row_sigma, col_sigma)

        # T prior.  This one is flat, but another uninformative you might
        # try is the two-sided error function (TwoSidedErf)

        Tminval = -1.0 # arcsec squared
        Tmaxval = 1000
        T_prior = ngmix.priors.FlatPrior(Tminval, Tmaxval)

        # similar for flux.  Make sure the bounds make sense for
        # your images

        Fminval = -1.e1
        Fmaxval = 1.e5
        F_prior = ngmix.priors.FlatPrior(Fminval, Fmaxval)

        # now make a joint prior.  This one takes priors
        # for each parameter separately
        priors = ngmix.joint_prior.PriorSimpleSep(
        cen_prior,
        g_prior,
        T_prior,
        F_prior)

        return priors

    def _make_psf(self,psf_pixel_scale = 0.01,nx = 256,ny = 256):

        """
        for debugging: make a GalSim PSF from scratch, 
        based on the one implemented in simulations
        """
        import galsim

        jitter_fwhm = 0.1
        jitter = galsim.Gaussian(flux=1., fwhm=jitter_fwhm)

        lam_over_diam = 0.257831 # units of arcsec
        aberrations = np.zeros(38)             # Set the initial size.
        aberrations[0] = 0.                       # First entry must be zero
        aberrations[1] = -0.00305127
        aberrations[4] = -0.02474205              # Noll index 4 = Defocus
        aberrations[11] = -0.01544329             # Noll index 11 = Spherical
        aberrations[22] = 0.00199235
        aberrations[26] = 0.00000017
        aberrations[37] = 0.00000004

        optics = galsim.OpticalPSF(lam=625,diam=0.5,
                                       obscuration=0.38, nstruts=4,
                                       strut_angle=(90*galsim.degrees), strut_thick=0.087,
                                       aberrations=aberrations)

        psf = galsim.Convolve([jitter,optics])
        gpsf_cutout = psf.drawImage(scale=psf_pixel_scale,nx=nx,ny=ny)

        return gpsf_cutout

    def _get_jacobians(self, source_id=None):
        jlist = self.medsObj.get_jacobian_list(source_id)
        jac = [ngmix.Jacobian(row=jj['row0'],col=jj['col0'],dvdrow = jj['dvdrow'],\
                                  dvdcol=jj['dvdcol'],dudrow=jj['dudrow'],dudcol=jj['dudcol']) for jj in jlist]

        return jac

    def _get_source_observations(self,source_id = None,psf_noise = 1e-6):
        jaclist = self._get_jacobians(source_id)
        psf_cutouts = self.medsObj.get_cutout_list(source_id, type='psf')
        weight_cutouts = self.medsObj.get_cutout_list(source_id, type='weight')
        image_cutouts = self.medsObj.get_cutout_list(source_id, type='image')
        image_obslist = ngmix.observation.ObsList()

        for i in range(len(image_cutouts)):

            # Apparently it likes to add noise to the psf.
            this_psf = psf_cutouts[i] + psf_noise * np.random.randn(psf_cutouts[i].shape[0],psf_cutouts[i].shape[1])
            this_psf_weight = np.zeros_like(this_psf) + 1./psf_noise**2

            this_image = image_cutouts[i]

            # Treat sky background variance as a Poisson distribution, e.g.
            #     - mean bkg = 0.048*600
            #     - std_dev = sqrt(bkg) = 5.3
            #     - sky_sigma = std_dev**2 = 25.1

            sky_sigma = (2)**2
            this_weight = np.zeros_like(this_image)+ 1./sky_sigma

            jj = jaclist[i]

            psfObs = ngmix.observation.Observation(this_psf,
                                                   weight=this_psf_weight,
                                                   jacobian=jj)

            imageObs = ngmix.observation.Observation(this_image,
                                                     weight=this_weight,
                                                     jacobian=jj,
                                                     psf=psfObs)
            #imageObs.psf_nopix = imageObs.psf

            image_obslist.append(imageObs)

        # Handled by Plotter clas now
        # Make an average cutout image for diagnostics plotting
        # self._make_imc(source_id=source_id)

        return image_obslist

class SuperBITPlotter(object):

    def setup_jdict_list(self, index_start, index_end, meds_list):
        '''
        Need to grab jacobian dict list for later plotting in parallel
        '''

        self.jdict_list = []

        for i in range(index_start, index_end):
            self.jdict_list.append(meds_list.get_jacobian_list(i)[0])

        return

    @staticmethod
    def _make_imc(source_id, meds_list):
        imc_arr = []
        for imc in range(meds_list[source_id]['ncutout']):
                imc_arr.append(meds_list.get_cutout(source_id, imc, type='image'))

        return np.mean(imc_arr, axis=0)

    @classmethod
    def make_imc_list(cls, index_start, index_end, meds_list):
        '''
        assumes you have already checked that make_plots is True, as
        it will take time to make images
        '''

        imc_list = []

        for i in range(index_start, index_end):
            imc_list.append(cls._make_imc(i, meds_list))

        return imc_list

    def _get_adaptmom(self, im, jac, vb=False):

        """
        Utility function to return the result of a galsim.FindAdaptiveMom()
        fit for an arbitrary 2x2 ndarray
        """

        galsim_im = galsim.Image(im,scale=jac.get_scale())
        guess_centroid = galsim.PositionD(galsim_im.true_center.x-1, galsim_im.true_center.y-1)
        try:

            hsm_fit = galsim_im.FindAdaptiveMom(guess_centroid=guess_centroid)
            hsm_sigma = hsm_fit.moments_sigma * .144
            hsm_g1 = hsm_fit.observed_shape.g1
            hsm_g2 = hsm_fit.observed_shape.g2

        except galsim.errors.GalSimHSMError:

            if vb is True:
                print('HSM fit failed; source is likely spurious')
            hsm_sigma = -9999; hsm_g1 = -9999; hsm_g2 = -9999

        outdict = {'hsm_sigma':hsm_sigma, 'hsm_g1':hsm_g1, 'hsm_g2':hsm_g2}

        return outdict

    def make_plots(self, imc, gmix, jac, outname, verbose=False):
        """
        Method to make plots of cutout, ngmix psf-convolved
        galaxy model, and residuals, all with galaxy size information
        (Gaussian sigma, ~FWHM/2.355) and colorbars.
        """

        cutout_avg = imc
        model_image = gmix.make_image(cutout_avg.shape, jacobian=jac)

        model_hsm_sigma = self._get_adaptmom(model_image,jac,verbose)['hsm_sigma']
        real_hsm_sigma = self._get_adaptmom(cutout_avg,jac,verbose)['hsm_sigma']

        y, x = gmix.get_cen()
        y /= jac.get_scale()
        x /= jac.get_scale()

        fig, (ax1, ax2, ax3) = plt.subplots(nrows=1, ncols=3, figsize=(12,4))

        #norm=colors.PowerNorm(gamma=1.2)
        real=ax1.imshow(cutout_avg, vmax=(cutout_avg.max()*.90))
        ax1.plot((x+(cutout_avg.shape[1]-1)/2), (y+(cutout_avg.shape[0]-1)/2), 'xr', \
                markersize=11, label='gmix_cen+row0/col0')

        ax1.axvline((cutout_avg.shape[0]-1)*0.5, color='black')
        ax1.axhline((cutout_avg.shape[1]-1)*0.5, color='black')
        ax1.plot(jac.col0, jac.row0, 'sw', fillstyle='none', \
                markersize=10, label='row0/col0')
        ax1.legend()
        ax1.set_title('cutout HSM sigma = %.3f' % real_hsm_sigma,fontsize=10)

        divider = make_axes_locatable(ax1)
        cax = divider.append_axes("right", size="5%", pad=0.05)
        plt.colorbar(real, cax=cax)

        model=ax2.imshow(model_image, vmax=(cutout_avg.max()*.90))
        ax2.plot((x+(cutout_avg.shape[0]-1)/2), (y+(cutout_avg.shape[1]-1)/2), 'xr', \
                markersize=11, label='gmix_cen+row0/col0')
        ax2.axvline((cutout_avg.shape[0]-1)*0.5, color='black')
        ax2.axhline((cutout_avg.shape[1]-1)*0.5, color='black')
        ax2.plot(jac.col0, jac.row0, 'sw', fillstyle='none', \
                     markersize=10, label='row0/col0')
        ax2.legend()
        ax2.set_title('model HSM sigma = %.3f' % model_hsm_sigma,fontsize=10)

        divider = make_axes_locatable(ax2)
        cax = divider.append_axes('right', size='5%', pad=0.05)
        plt.colorbar(model, cax=cax)

        resi = ax3.imshow(cutout_avg - model_image)
        ax3.set_title('cutout - model',fontsize=10)

        divider = make_axes_locatable(ax3)
        cax = divider.append_axes('right', size='5%', pad=0.05)
        plt.colorbar(resi, cax=cax)
        fig.tight_layout()

        fig.savefig(outname)
        plt.close(fig)

        return 0

def set_seed(config):
    seed = int(time.time())
    config['seed'] = seed

    return

def make_output_table(outfilename, mcal, identifying, return_table=False):
    """
    Create an output table and add in metacal/ngmix galaxy fit parameters

    :mcal: array containing output parameters from the metacalibration fit
    :identifying: contains ra, dec and coadd cat ID for cross-matching if needed

    TO DO: find more compact/pythonic way of doing this exercise?
    """

    join_tab = mcal_dict2tab(mcal, identifying)

    write_output_table(outfilename, join_tab)

    if return_table is True:
        return join_tab
    else:
        return 0

def mcal_dict2tab(mcal, ident):
    '''
    mcal is the dict returned by ngmix.get_metacal_result()

    ident is an array with MEDS identification info like id, ra, dec
    not returned by the function
    '''

    # Annoying, but have to do this to make Table from scalars
    for key, val in ident.items():
        ident[key] = np.array([val])

    tab_names = ['noshear', '1p', '1m', '2p', '2m']
    for name in tab_names:
        tab = mcal[name]

        for key, val in tab.items():
            tab[key] = np.array([val])

        mcal[name] = tab

    id_tab = Table(data=ident)

    tab_noshear = Table(mcal['noshear'])
    tab_1p = Table(mcal['1p'])
    tab_1m = Table(mcal['1m'])
    tab_2p = Table(mcal['2p'])
    tab_2m = Table(mcal['2m'])

    join_tab = hstack([id_tab, hstack([tab_noshear, tab_1p,  tab_1m, tab_2p, tab_2m], \
                                      table_names=tab_names)])

    return join_tab

def write_output_table(outfilename, tab):
    tab.write(outfilename, format='fits', overwrite=True)

    return


# def mcal_dict2table(mcal, ident):
#     '''
#     mcal is the dict returned by ngmix.get_metacal_result()
#     ident is an array with MEDS identification info like id, ra, dec
#     not returned by the function
#     '''

#     id_tab = Table(data=ident)

#     tab_noshear = Table(mcal['noshear'])
#     tab_1p = Table(mcal['1p'])
#     tab_1m = Table(mcal['1m'])
#     tab_2p = Table(mcal['2p'])
#     tab_2m = Table(mcal['2m'])

#     return mcal_arr

def mp_fit_one(source_id, jaclist, obslist, prior, logprint, pars=None):
    """
    Multiprocessing version of original _fit_one()

    Method to perfom metacalibration on an object. Returns the unsheared ellipticities
    of each galaxy, as well as entries for each shear step

    inputs:
    - source_id: MEDS ID
    - jaclist: Jacobian list for MEDS object of given ID
    - obslist: Observation list for MEDS object of given ID
    - prior: ngmix mcal priors
    - pars: mcal running parameters

    TO DO: add a label indicating whether the galaxy passed the selection
    cuts for each shear step (i.e. no_shear,1p,1m,2p,2m).
    """

    index = source_id

    if pars is None:
        mcal_shear = 0.01
        lm_pars = {'maxfev':2000, 'xtol':5.0e-5, 'ftol':5.0e-5}
        max_pars = {'method':'lm','lm_pars':lm_pars,'find_center':True}
        metacal_pars={'step':mcal_shear}

    Tguess = 4*jaclist[0].get_scale()**2
    ntry = 3
    psf_model = 'gauss' # should come up with diagnostics for PSF quality
    gal_model = 'exp'

    # Run the actual metacalibration fits on the observed galaxies
    mcb = ngmix.bootstrap.MaxMetacalBootstrapper(obslist)
    mcb.fit_metacal(psf_model, gal_model, max_pars, Tguess, prior=prior,
                    ntry=ntry, metacal_pars=metacal_pars)
    mcal_res = mcb.get_metacal_result() # this is a dict

    if logprint.vb is True:
        R1 = (mcal_res['1p']['g'][0] - mcal_res['1m']['g'][0])/(2*mcal_shear)
        R2 = (mcal_res['2p']['g'][1] - mcal_res['2m']['g'][1])/(2*mcal_shear)
        logprint(f'R1: {R1:.3} \nR2:{R2:.3} ')

    # To generate a model image, these calls do need to be here
    mcb.fit_psfs(psf_model, 1.)
    mcb.fit_max(gal_model, max_pars, prior=prior)

    mcal_fit = mcb.get_max_fitter()

    return mcal_res, mcal_fit

def setup_obj(i, meds_obj):
    '''
    Setup object property dictionary used to compile fit params later on
    '''

    # Mcal object properties
    obj = {}

    obj['meds_indx'] = i
    obj['id'] = meds_obj['id']
    obj['ra'] = meds_obj['ra']
    obj['dec'] = meds_obj['dec']

    return obj

def mp_run_fit(i, obj, jaclist, obslist, prior, imc, plotter, config, logprint):
    '''
    parallelized version of original ngmix_fit_superbit3 code

    i: MEDS indx

    returns ...
    '''

    start = time.time()

    # ForkedPdb().set_trace()

    logprint(f'Starting fit for obj {i}')

    try:
        # mcal_res: the bootstrapper's get_mcal_result() dict
        # mcal_fit: the mcal model image
        mcal_res, mcal_fit = mp_fit_one(i, jaclist, obslist, prior, logprint)

        # Ain some identifying info like (ra,dec), id, etc.
        # for key in obj.keys():
        #     mcal_res[key] = obj[key]

        # convert result dict to a formatted table
        # obj here is the "identifying" table
        mcal_tab = mcal_dict2tab(mcal_res, obj)

        end = time.time()
        logprint(f'Fitting and conversion took {end-start} seconds')

        if config['make_plots'] is True:
            image_cutout = imc
            jdict = plotter.jdict_list[i]
            jac = ngmix.Jacobian(row=jdict['row0'],
                                 col=jdict['col0'],
                                 dvdrow=jdict['dvdrow'],
                                 dvdcol=jdict['dvdcol'],
                                 dudrow=jdict['dudrow'],
                                 dudcol=jdict['dudcol']
                                 )
            try:
                gmix = mcal_fit.get_convolved_gmix()
                filen = 'diagnostics-' + str(int(i)) + '.png'
                outname = os.path.join(config['im_savedir'], filen)
                plotter.make_plots(imc, gmix, jac, outname, logprint.vb)

                logprint('Plots made')

            except:
                # EM probably failed
                logprint('Bad gmix model, no image made')

    except Exception as e:
        logprint(e)
        logprint('object {i} failed, skipping...')

        mcal_tab = None

    end = time.time()

    logprint(f'Total runtime for object was {end-start} seconds')

    return mcal_tab

def main():

    args = parser.parse_args()

    vb = args.verbose # if True, prints out values of R11/R22 for every galaxy
    medsfile = args.medsfile
    outfilename = args.outfile
    index_start  = args.start
    index_end = args.end
    make_plots = args.plot
    nproc = args.n
    meds_info = {'meds_file':medsfile}
    identifying = {'meds_index':[], 'id':[], 'ra':[], 'dec':[]}
    mcal = {'noshear':[], '1p':[], '1m':[], '2p':[], '2m':[]}

    homedir = './'

    # Set up for saving files
    im_savedir = os.path.join(homedir, 'diagnostics_plots/')
    if make_plots is True:
        if not os.path.isdir(im_savedir):
            cmd = 'mkdir -p %s' % im_savedir
            os.system(cmd)

    # Added to handle rng initialization
    # Could put everything through here instead
    config = {}
    config['medsfile'] = medsfile
    config['outfile'] = outfilename
    config['verbose'] = vb
    config['make_plots'] = make_plots
    config['im_savedir'] = im_savedir
    config['nproc'] = nproc
    set_seed(config)

    # Setup log in same dir as meds file
    logdir = os.path.dirname(config['medsfile'])
    logfile = 'mcal_fitting.log'
    log = sb_utils.setup_log(logfile, logdir=logdir)
    logprint = sb_utils.LogPrint(log, vb)

    log.info(f'MEDS file: {medsfile}')
    log.info(f'index start, end: {index_start}, {index_end}')
    log.info(f'outfile: {outfilename}')
    log.info(f'make_plots: {make_plots}')
    log.info(f'im_savedir: {im_savedir}')
    log.info(f'nproc: {nproc}')
    log.info(f'vb: {vb}')
    log.info(f'seed: {config["seed"]}')

    BITfitter = SuperBITNgmixFitter(config, meds_info) # MEDS file

    priors = BITfitter._get_priors()

    if index_start is None:
        index_start = 0
    if index_end is None:
        index_end = len(BITfitter.catalog)

    # Needed for making plots on each worker
    plotter = SuperBITPlotter()

    # Setup jacobian dict list here for parallelization
    plotter.setup_jdict_list(index_start, index_end, BITfitter.medsObj)

    if make_plots is True:
        imc_list = plotter.make_imc_list(index_start, index_end, BITfitter.medsObj)
    else:
        imc_list = [None for i in range(index_start, index_end)]

    logprint(f'Starting metacal fitting with {nproc} cores')

    start = time.time()

    # for no multiprocessing:
    # mcal_res = []
    # for i in range(index_start, index_end):
    #     mcal_res.append(mp_run_fit(
    #                       i,
    #                       setup_obj(i, BITfitter.medsObj[i]),
    #                       BITfitter._get_jacobians(i),
    #                       BITfitter._get_source_observations(i),
    #                       priors,
    #                       imc_list[i-index_start],
    #                       plotter,
    #                       config,
    #                       logprint)
    #                     )

    # mcal_res = vstack(mcal_res)

    # for multiprocessing:
    with Pool(nproc) as pool:
        mcal_res = vstack(pool.starmap(mp_run_fit,
                                       [(i,
                                         setup_obj(i, BITfitter.medsObj[i]),
                                         BITfitter._get_jacobians(i),
                                         BITfitter._get_source_observations(i),
                                         priors,
                                         imc_list[i-index_start],
                                         plotter,
                                         config,
                                         logprint
                                         ) for i in range(index_start, index_end)
                                        ]
                                       )
                          )

    end = time.time()

    T = end - start
    logprint(f'Total fitting and stacking time: {T} seconds')

    N = index_end - index_start
    logprint(f'{T/N} seconds per object (wall time)')
    logprint(f'{T/N*nproc} seconds per object (CPU time)')

    out = os.path.join(homedir, outfilename)

    logprint(f'Writing results to {out}')

    write_output_table(out, mcal_res)

    logprint('Done!')

    return

if __name__ == '__main__':

    try:
        main()
    except:
        thingtype, value, tb = sys.exc_info()
        traceback.print_exc()
        pdb.post_mortem(tb)